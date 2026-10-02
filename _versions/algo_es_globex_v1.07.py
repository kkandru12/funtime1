"""Globex strategy sleeve -- wires algo_es/strategies into the main loop.

v1.07 2026-10-02 [5DMA-M1 + NYGEX] 5DMA-STRUCT enters intraday (KK: daily
    close is too late; every touch counts): an M1 bar touches the 5DMA, entry
    on the M1 close back on the trend side; re-arms after price trades away.
    NY session (09:30-16:00 ET) with GEX active: the Globex sleeve does NOT
    enter -- the dominant-wall fade/breakout sleeves own the single ES slot
    there (KK: "if it interferes with dominant wall logic, limit to Globex").
    Strategies still see every bar so their state stays current.
    ES_5DMA_ENTRY=daily restores the v1.06 daily-close entry.

v1.06 2026-10-02 [D1WARM] 5DMA-STRUCT no longer fires on startup: the
    warm-up consumes every closed daily bar and only a NEW daily bar can signal.

v1.02 2026-10-01 [GLOBEXWIRE] First build.
    The five ported Apex Globex strategies (VOB, SQUEEZE, BB-2C, DMA-520,
    SMA-CROSS) plus 5DMA-STRUCT existed as modules but main.py never called
    them.  This sleeve:
      - pulls MT5 bars for every timeframe they need (M1, M15, H1, H4, D1)
        every ES_GLOBEX_REFRESH_SEC (default 30 s)
      - builds each strategy's own `state` dict (the key "bars" means M15
        for SMA-CROSS but H4 structure bars for BB-2C -- see PORT_NOTES.md)
      - calls every enabled strategy ONCE per newly closed M1 bar, flat or
        not, so their state machines never miss a bar
      - returns the first valid signal as a main.py candidate dict
        (single target = tp2, stop = the strategy's stop)
    Strategies run around the clock by default (ES_GLOBEX_ALL_HOURS=1);
    =0 restores the Apex 09:30-13:00 ET stand-aside.
    A signal whose stop/target is already on the wrong side of the live
    price is dropped (GLOBEX_REJECT), never sent.
    SQUEEZE stays inert unless the bridge publishes vanna/charm scores.
"""
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

import config
from strategies import STRATEGIES
from strategies.fivedma_struct import FiveDMAStruct, Bar as FBar
from strategy import true_risk_dollars

log = logging.getLogger("algo_es.globex")

# bars pulled per timeframe (enough history for every strategy's warm-up)
PULL = {"m1": 120, "m15": 260, "h1": 300, "h4": 120, "d1": 40}
ORDER = ("vob", "squeeze", "bb2c", "dma520", "smacross", "fivedma")


def _closed(bars: List[Dict]) -> List[Dict]:
    """Drop the forming bar (MT5 pos 0 = last element)."""
    return bars[:-1] if bars else []


class GlobexSleeve:
    def __init__(self, emit, enabled: Optional[str] = None,
                 all_hours: Optional[bool] = None):
        self.emit = emit
        names = [n.strip().lower() for n in
                 (enabled if enabled is not None else config.GLOBEX_STRATEGIES).split(",")
                 if n.strip()]
        all_hours = config.GLOBEX_ALL_HOURS if all_hours is None else all_hours
        cfg = {"globex_only": not all_hours}
        self.strats = {}
        for n in ORDER:
            if n not in names:
                continue
            if n == "fivedma":
                self.strats[n] = FiveDMAStruct()
            elif n in STRATEGIES:
                self.strats[n] = STRATEGIES[n](dict(cfg))
        unknown = [n for n in names if n not in ORDER]
        if unknown:
            log.warning("unknown ES_GLOBEX_STRATEGIES entries ignored: %s", unknown)
        self.bars: Dict[str, List[Dict]] = {k: [] for k in PULL}
        self._pulled = 0.0
        self._last_m1 = None       # time of the last closed M1 evaluated
        self._last_d1 = None       # time of the last closed D1 fed to fivedma

    # ------------------------------------------------------------ data ---
    async def refresh(self, mt5data, force: bool = False):
        if not force and time.time() - self._pulled < config.GLOBEX_REFRESH_SEC:
            # M1 is cheap and drives the cadence: always refresh it
            self.bars["m1"] = await mt5data.rates("m1", PULL["m1"]) or self.bars["m1"]
            return
        self._pulled = time.time()
        for tf, n in PULL.items():
            got = await mt5data.rates(tf, n)
            if got:
                self.bars[tf] = got

    def load(self, bars: Dict[str, List[Dict]]):
        """Inject bars directly (tests / replay). Same shape as refresh()."""
        for k, v in bars.items():
            self.bars[k] = v

    # ------------------------------------------------------------ state --
    def _state(self, name: str, now: datetime, spot: float, gex) -> Dict:
        m1 = _closed(self.bars["m1"])
        d1 = _closed(self.bars["d1"])
        st = {"now_et": now, "last_price": spot, "bars_m1": m1,
              "daily_closes": [b["close"] for b in d1]}
        g = _gex_dict(gex)
        if g:
            st["gex"] = g
        if name == "vob":
            st["bars_h1"] = _closed(self.bars["h1"])
        elif name == "bb2c":
            st["bars"] = _closed(self.bars["h4"])        # structure TF 4h
            st["entry_bars"] = _closed(self.bars["h1"])  # entry TF 1h
            st["struct_tf"] = "4h"
        elif name == "dma520":
            # m1 entry mode reads the FORMING tf candle -> keep it last
            st["tf_bars"] = {"1h": list(self.bars["h1"]), "4h": list(self.bars["h4"])}
        elif name == "smacross":
            st["bars"] = _closed(self.bars["m15"])
        return st

    # ------------------------------------------------------------ run ----
    def step(self, now: datetime, spot: float, gex) -> List[Dict]:
        """Run every strategy once per NEW closed M1 bar. Returns the list of
        raw signals as dicts {name, signal}; [] when nothing new."""
        m1 = _closed(self.bars["m1"])
        if not m1 or not spot:
            return []
        last = m1[-1]
        if self._last_m1 is not None and last["time"] <= self._last_m1:
            return []
        self._last_m1 = last["time"]
        block = self.ny_gex_block(now, gex)
        out = []
        for name, s in self.strats.items():
            try:
                if name == "fivedma" and config.FIVEDMA_ENTRY == "m1":
                    d1_all = self.bars["d1"]
                    if len(d1_all) < 3:
                        continue
                    s.load_days([_fbar(b) for b in d1_all[:-1]])   # completed days
                    sig = s.on_m1(last["high"], last["low"], last["close"],
                                  close_mode=not block)   # blocked: track only
                elif name == "fivedma":
                    d1 = _closed(self.bars["d1"])
                    if not d1 or (self._last_d1 is not None
                                  and d1[-1]["time"] <= self._last_d1):
                        continue
                    if self._last_d1 is None:
                        # [v1.06 D1WARM] warm up on ALL closed daily bars and
                        # signal only on the NEXT one. v1.02 judged the last
                        # closed day as new, so every restart re-fired a stale
                        # 5DMA signal (seen live 2026-10-02 00:25).
                        for b in d1:
                            s.on_bar(_fbar(b))
                            s._in_position = False
                        self._last_d1 = d1[-1]["time"]
                        continue
                    self._last_d1 = d1[-1]["time"]
                    sig = s.on_bar(_fbar(d1[-1]))
                else:
                    sig = s.on_bar(last, self._state(name, now, spot, gex))
            except Exception as e:  # noqa: BLE001 - one bad strategy never stops the loop
                log.warning("globex %s raised %s: %s", name, type(e).__name__, e)
                continue
            if sig and block:
                self.on_not_taken(name)          # NY + GEX: wall sleeves only
                continue
            if sig:
                out.append({"name": name, "signal": sig})
        return out

    @staticmethod
    def ny_gex_block(now: datetime, gex) -> bool:
        """[v1.07 NYGEX] True in the NY session (09:30-16:00 ET) while GEX is
        active: the dominant-wall sleeves own the ES slot, Globex stands aside."""
        t = (now.hour, now.minute)
        return gex is not None and (9, 30) <= t < (16, 0)

    def to_candidate(self, name: str, sig, spot: float, gex) -> Optional[Dict]:
        """Signal -> main.py candidate dict, or None when the levels no longer
        make sense against the live price."""
        long_ = str(sig.side).lower() in ("buy", "long")
        side = "long" if long_ else "short"
        stop, tgt = float(sig.stop_px), float(sig.target_px)
        ok = (stop < spot < tgt) if long_ else (tgt < spot < stop)
        if not ok:
            return None
        stop_pts = abs(spot - stop)
        qty = max(1, int(config.GLOBEX_QTY))
        return {"trigger": "globex_" + name, "sleeve": "globex",
                "strategy": getattr(sig, "strategy_name", name),
                "reason": getattr(sig, "reason", ""),
                "side": side, "wall": float(sig.entry_px), "qty": qty,
                "risk_usd": true_risk_dollars(stop_pts, qty),
                "spot": spot, "regime": getattr(gex, "regime", None),
                "stop_pts": round(stop_pts, 2), "stop_px": stop,
                "entry_px": float(sig.entry_px),
                "tp1_px": None, "tp2_px": tgt}

    def on_position_closed(self, trigger: str):
        """5DMA-STRUCT keeps an internal one-position flag; clear it."""
        if trigger == "globex_fivedma" and "fivedma" in self.strats:
            self.strats["fivedma"].on_exit()

    def on_not_taken(self, name: str):
        if name == "fivedma" and "fivedma" in self.strats:
            self.strats["fivedma"].on_exit()


def _fbar(b: Dict) -> FBar:
    return FBar(str(b["time"]), b["open"], b["high"], b["low"], b["close"])


def _gex_dict(gex) -> Optional[Dict]:
    """BridgeGex -> the plain dict the strategies read. Walls are ES-space."""
    if gex is None:
        return None
    try:
        d = {"call_wall": gex.call_wall, "put_wall": gex.put_wall,
             "flip": gex.flip, "is_fresh": not bool(gex.stale),
             "is_negative_regime": gex.gamma_regime() == "-"}
    except Exception:  # noqa: BLE001
        return None
    for k in ("vanna_score", "vanna_dir", "charm_score", "charm_dir"):
        v = getattr(gex, k, None)
        if v is not None:
            d[k] = v
    return d

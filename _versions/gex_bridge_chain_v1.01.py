"""v1.01 2026-10-02 [NOSNAPGENERIC] IBKR rejects snapshot=True with generic
    ticks (Error 321 "Snapshot market data subscription is not applicable to
    generic ticks"), so the morning OI scan (tick 101) and the wing sweeps
    (100,101,106,107) never received data -- seen live 2026-10-02 09:32.
    Both now use a BRIEF STREAMING request: subscribe (snapshot=False), wait
    until the wanted field arrives or BRIDGE_SNAPSHOT_DWELL passes, cancel.
    At most BRIDGE_BRIEF_MAX_LINES (default 15) brief lines are open at once,
    so peak lines stay ~63 streaming + 15 brief < 100 cap.
    [SPXFIRST] ensure_spx() subscribes the SPX line before the OI scan (main
    v1.09) -- spot() was polled before SPX was ever subscribed. spot() now
    skips NaN marketPrice and falls back to last/close.

Market-data layer: chain discovery, morning OI snapshot, line-budgeted
streaming, dynamic window re-centering, wing sweeps, 1-min bars, AllLast flow.

LINE BUDGET (hard cap 100, target <=75 sustained):
  1   SPX underlying streaming
  42  active window: 5-pt strikes, spot +/-50, both rights (streaming)
  20  crush band: 55-95 pts OTM, 10-pt steps, both sides, both rights (streaming)
  10  reserved: AllLast tick-by-tick on held position + top candidates
  --  sustained 63, peak with AllLast 73.

OI is frozen intraday (verified): ONE morning snapshot of all 0DTE strikes via
paced one-shot snapshot requests, then unsubscribe. GEX = frozen OI x live gamma.

Greeks are IBKR-sent (bid/ask model Greeks from generic ticks 106/107) - we do
NOT recompute Greeks in Python.
"""
import asyncio
import logging
import statistics
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from ib_insync import IB, Contract, Index, Ticker

import config
from pacer import Pacer

log = logging.getLogger("algo.chain")

# priority: lower number = shed last
PRIO_SPX = 0
PRIO_ACTIVE = 1
PRIO_CRUSH = 2
PRIO_ALLLAST = 3


def _is_5pt(k: float) -> bool:
    return abs(k - round(k / 5) * 5) < 0.01


def _greeks_from_ticker(t: Ticker):
    g = t.askGreeks or t.bidGreeks or t.lastGreeks or t.modelGreeks
    if g is None:
        return None
    try:
        return dict(iv=float(g.impliedVol), delta=float(g.delta),
                    gamma=float(g.gamma), theta=float(g.theta),
                    vega=float(g.vega))
    except (TypeError, ValueError):
        return None


def _oi_from_ticker(t: Ticker, right: str) -> float:
    v = t.callOpenInterest if right == "C" else t.putOpenInterest
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


class ChainStream:
    def __init__(self, ib: IB, pacer: Pacer):
        self.ib = ib
        self.pacer = pacer
        self._brief_sem = None          # [v1.01] created on the running loop
        self.contracts: dict[tuple[float, str], Contract] = {}
        self.expiry = ""
        self.stream: dict[tuple[float, str], Ticker] = {}
        self.stream_prio: dict[tuple[float, str], int] = {}
        self.spx_ticker: Ticker | None = None
        self.alllast: dict[tuple[float, str], Ticker] = {}
        self.alllast_seen: dict[tuple[float, str], int] = {}
        self.oi: dict[tuple[float, str], float] = {}
        self.wing_greeks: dict[tuple[float, str], dict] = {}
        self.ask_hist: dict[tuple[float, str], deque] = defaultdict(
            lambda: deque(maxlen=config.ASK_Z_LOOKBACK_MIN))
        self.iv_hist: deque = deque(maxlen=config.IV_LOOKBACK_MIN)
        self._bar_minute: datetime | None = None
        self._bar_accum: dict[tuple[float, str], list] = defaultdict(list)
        self._iv_accum: list = []
        self.flow: dict[tuple[float, str], deque] = defaultdict(lambda: deque(maxlen=500))
        self.lines_used = 0

    async def discover(self, expiry_yyyymmdd: str) -> int:
        """One reqContractDetails returns the whole 0DTE SPXW chain."""
        self.expiry = expiry_yyyymmdd
        probe = Contract(secType="OPT", symbol=config.UNDERLYING,
                         lastTradeDateOrContractMonth=expiry_yyyymmdd,
                         exchange=config.EXCHANGE, tradingClass=config.TRADING_CLASS,
                         currency="USD")
        details = await self.ib.reqContractDetailsAsync(probe)
        n = 0
        for d in details:
            c = d.contract
            if c.secType != "OPT":
                continue
            self.contracts[(float(c.strike), c.right)] = c
            n += 1
        log.info("discovered %d 0DTE SPXW contracts for %s", n, expiry_yyyymmdd)
        return n

    async def _brief(self, contract: Contract, tick_list: str, done):
        """[v1.01 NOSNAPGENERIC] Short streaming request; returns the Ticker
        after `done(ticker)` is true or the dwell expires. Line always freed."""
        if self._brief_sem is None:
            self._brief_sem = asyncio.Semaphore(config.BRIEF_MAX_LINES)
        async with self._brief_sem:
            await self.pacer.acquire()
            t = self.ib.reqMktData(contract, genericTickList=tick_list,
                                   snapshot=False, regulatorySnapshot=False)
            try:
                waited = 0.0
                while waited < config.SNAPSHOT_DWELL:
                    await asyncio.sleep(0.25)
                    waited += 0.25
                    try:
                        if done(t):
                            break
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                try:
                    self.ib.cancelMktData(contract)
                except Exception:  # noqa: BLE001
                    pass
            return t

    async def _snapshot_one(self, contract: Contract, tick_list: str):
        t = await self._brief(contract, tick_list,
                              lambda x: _greeks_from_ticker(x) is not None)
        return {"bid": t.bid, "ask": t.ask, "greeks": _greeks_from_ticker(t)}

    async def morning_oi_snapshot(self) -> dict:
        """ONE full-chain OI scan (brief streaming requests, <= BRIEF_MAX_LINES held)."""
        log.info("morning OI snapshot: %d contracts", len(self.contracts))
        oi: dict[tuple[float, str], float] = {}

        async def one(item):
            key, contract = item
            try:
                r = key[1]
                t = await self._brief(contract, "101",
                                      lambda x: _oi_from_ticker(x, r) > 0)
                oi[key] = _oi_from_ticker(t, key[1])
            except Exception as e:
                log.warning("OI snapshot failed %s: %s", key, e)

        await asyncio.gather(*(one(it) for it in self.contracts.items()))
        self.oi = oi
        covered = sum(1 for v in oi.values() if v > 0)
        log.info("OI snapshot done: %d/%d reporting OI>0", covered, len(oi))
        return oi

    async def wing_sweep(self, spot: float):
        """Paced snapshot sweep of non-streaming strikes within range, for GEX
        quote/Greek completeness. Never streams wings."""
        want = [k for k in self.contracts
                if abs(k[0] - spot) <= config.WING_SWEEP_RANGE and k not in self.stream]
        log.info("wing sweep: %d strikes", len(want))

        async def one(key):
            try:
                d = await self._snapshot_one(self.contracts[key], config.GENERIC_TICKS)
                if d["greeks"]:
                    self.wing_greeks[key] = d["greeks"]
            except Exception as e:
                log.debug("wing sweep miss %s: %s", key, e)

        await asyncio.gather(*(one(k) for k in want))
        log.info("wing sweep done: greeks for %d wing strikes", len(self.wing_greeks))

    def _lines(self) -> int:
        return (1 if self.spx_ticker else 0) + len(self.stream) + len(self.alllast)

    async def _sub_stream(self, key, prio):
        if key in self.stream:
            return
        await self.pacer.acquire()
        t = self.ib.reqMktData(self.contracts[key],
                               genericTickList=config.GENERIC_TICKS,
                               snapshot=False, regulatorySnapshot=False)
        self.stream[key] = t
        self.stream_prio[key] = prio

    def _unsub_stream(self, key):
        if key in self.stream:
            self.ib.cancelMktData(self.contracts[key])
            del self.stream[key]
            self.stream_prio.pop(key, None)
        if key in self.alllast:
            self.ib.cancelTickByTickData(self.contracts[key])
            del self.alllast[key]
            self.alllast_seen.pop(key, None)

    def enforce_budget(self):
        """Shed lowest-priority lines near the cap. AllLast goes before any
        chain line; chain priority: crush band, then active window, never SPX."""
        while self._lines() > config.LINE_HARD_CAP - 2:
            if self.alllast:
                key = next(iter(self.alllast))
                log.warning("shedding AllLast %s (budget)", key)
                self._unsub_stream(key)
                continue
            shed = [k for k, p in self.stream_prio.items() if p == PRIO_CRUSH]
            if not shed:
                shed = [k for k, p in self.stream_prio.items() if p == PRIO_ACTIVE]
            if not shed:
                log.error("cannot shed further; lines=%d", self._lines())
                break
            key = shed[0]
            log.warning("shedding streaming %s prio=%d (budget)", key, self.stream_prio[key])
            self._unsub_stream(key)

    def desired_sets(self, spot: float):
        active, crush = set(), set()
        for (k, r) in self.contracts:
            if abs(k - spot) <= config.ACTIVE_WINDOW_PTS and _is_5pt(k):
                active.add((k, r))
                continue
            d = k - spot
            otm = abs(d)
            if config.CRUSH_BAND_MIN_OTM <= otm <= config.CRUSH_BAND_MAX_OTM:
                if abs(otm - round(otm / config.CRUSH_BAND_STEP)
                       * config.CRUSH_BAND_STEP) < 0.01:
                    crush.add((k, r))
        return active, crush

    async def ensure_spx(self):
        """[v1.01 SPXFIRST] Subscribe the SPX index line once (idempotent)."""
        if self.spx_ticker is not None:
            return
        await self.pacer.acquire()
        spx = Index(config.UNDERLYING, config.EXCHANGE, "USD")
        await self.ib.qualifyContractsAsync(spx)
        self.spx_ticker = self.ib.reqMktData(spx, "", False, False)
        log.info("SPX streaming subscribed")

    async def start_streaming(self, spot: float):
        await self.ensure_spx()
        await self.recenter(spot, force=True)

    async def recenter(self, spot: float, force: bool = False):
        active, crush = self.desired_sets(spot)
        want = {k: PRIO_ACTIVE for k in active} | {k: PRIO_CRUSH for k in crush}
        for key in set(self.stream) - set(want):
            self._unsub_stream(key)
        added = 0
        for key, prio in want.items():
            if key not in self.stream:
                await self._sub_stream(key, prio)
                added += 1
        self.enforce_budget()
        self.lines_used = self._lines()
        log.info("recenter spot=%.1f active=%d crush=%d lines=%d (+%d)",
                 spot, len(active), len(crush), self.lines_used, added)

    # ---------------- lifecycle (bridge additions) ----------------
    def cancel_options(self):
        """Cancel all OPTION streaming lines but keep the SPX index line.

        Overnight transition: walls are frozen from the last published
        eval; this stops option flow cleanly while SPX (+ ES, subscribed
        separately) stays up.
        """
        for key in list(self.stream):
            self._unsub_stream(key)
        for key in list(self.alllast):
            self._unsub_stream(key)

    def stop_streaming(self):
        """Cancel everything including the SPX line (orderly shutdown)."""
        self.cancel_options()
        if self.spx_ticker is not None:
            try:
                self.ib.cancelMktData(self.spx_ticker.contract)
            except Exception:
                pass
            self.spx_ticker = None

    # ---------------- AllLast flow ----------------
    async def alllast_set(self, keys: list[tuple[float, str]]):
        """AllLast on <= ALLLAST_MAX_LINES: held position + top candidates."""
        want = set(keys[:config.ALLLAST_MAX_LINES])
        for key in list(self.alllast):
            if key not in want:
                self.ib.cancelTickByTickData(self.contracts[key])
                del self.alllast[key]
                self.alllast_seen.pop(key, None)
        for key in want:
            if key not in self.alllast and key in self.contracts:
                await self.pacer.acquire()
                t = self.ib.reqTickByTickData(self.contracts[key], "AllLast", 0, False)
                self.alllast[key] = t
                self.alllast_seen[key] = len(t.tickByTicks)
        self.enforce_budget()

    def drain_flow(self):
        """Drain new AllLast prints; tick-rule sign vs streaming quote."""
        for key, t in self.alllast.items():
            ticks = t.tickByTicks
            seen = self.alllast_seen.get(key, 0)
            if len(ticks) <= seen:
                continue
            st = self.stream.get(key)
            bid = st.bid if st and st.bid and st.bid > 0 else None
            ask = st.ask if st and st.ask and st.ask > 0 else None
            for tick in ticks[seen:]:
                price = getattr(tick, "price", None)
                size = getattr(tick, "size", 0) or 0
                ts = getattr(tick, "time", None)
                if price is None:
                    continue
                if ask and price >= ask:
                    s = 1
                elif bid and price <= bid:
                    s = -1
                else:
                    s = 0
                self.flow[key].append((ts, s * size))
            self.alllast_seen[key] = len(ticks)

    def flow_stats(self, key, window_sec=300):
        """(n_prints, signed_volume) over trailing window. Logged / tiebreak only."""
        now = datetime.now(timezone.utc).timestamp()
        n, sv = 0, 0
        for ts, ss in self.flow.get(key, ()):
            try:
                t = ts.timestamp() if hasattr(ts, "timestamp") else float(ts)
            except (TypeError, ValueError):
                continue
            if now - t <= window_sec:
                n += 1
                sv += ss
        return n, sv

    # ---------------- 1-minute bars ----------------
    def note_quotes(self, now_et: datetime):
        """Call each loop iteration. Rolls 1-min ask bars for crush-band
        contracts and ATM IV; z-scores need trailing 30-min series."""
        minute = now_et.replace(second=0, microsecond=0)
        if self._bar_minute is None:
            self._bar_minute = minute
        if minute != self._bar_minute:
            for key, asks in self._bar_accum.items():
                if asks:
                    self.ask_hist[key].append(statistics.median(asks))
            if self._iv_accum:
                self.iv_hist.append(statistics.median(self._iv_accum))
            self._bar_accum = defaultdict(list)
            self._iv_accum = []
            self._bar_minute = minute
        for key, t in self.stream.items():
            if self.stream_prio.get(key) in (PRIO_CRUSH, PRIO_ACTIVE) \
                    and t.ask and t.ask > 0:
                self._bar_accum[key].append(t.ask)
        iv = self.atm_iv()
        if iv:
            self._iv_accum.append(iv)
        self.drain_flow()

    def ask_zscore(self, key) -> float | None:
        h = self.ask_hist.get(key)
        if not h or len(h) < config.MIN_ASK_HISTORY:
            return None
        mu = statistics.mean(h)
        sd = statistics.pstdev(h)
        if sd <= 0:
            return None
        cur = h[-1]
        return (cur - mu) / sd

    # ---------------- state for strategy ----------------
    def spot(self) -> float | None:
        t = self.spx_ticker
        if t is None:
            return None
        # [v1.01] NaN is truthy: take the first FINITE positive price
        for px in (t.marketPrice(), t.last, t.close):
            try:
                if px is not None and float(px) > 0 and float(px) == float(px):
                    return float(px)
            except (TypeError, ValueError):
                continue
        return None

    def atm_iv(self) -> float | None:
        s = self.spot()
        if not s:
            return None
        best, bestd = None, 1e9
        for (k, r), t in self.stream.items():
            if r != "C":
                continue
            d = abs(k - s)
            if d < bestd:
                g = _greeks_from_ticker(t)
                if g and g["iv"] and 0.02 < g["iv"] < 3.0:
                    best, bestd = g["iv"], d
        return best

    def iv_ok(self) -> tuple[bool, str]:
        """IV-flat regime check: skip entries when ATM IV spiked."""
        if len(self.iv_hist) < config.MIN_ASK_HISTORY:
            return True, "warming-up"
        iv_now = self.iv_hist[-1]
        med = statistics.median(self.iv_hist)
        if med <= 0:
            return True, "no-iv"
        if iv_now > config.IV_SPIKE_RATIO * med:
            return False, f"iv-spike {iv_now:.2f} > {config.IV_SPIKE_RATIO}x median {med:.2f}"
        return True, "ok"

    def contract_state(self, key) -> dict | None:
        t = self.stream.get(key)
        if t is None:
            return None
        bid = t.bid if t.bid and t.bid > 0 else 0.0
        ask = t.ask if t.ask and t.ask > 0 else 0.0
        if ask <= 0:
            return None
        g = _greeks_from_ticker(t)
        if not g:
            return None
        return dict(key=key, strike=key[0], right="call" if key[1] == "C" else "put",
                    bid=bid, ask=ask, mid=(bid + ask) / 2,
                    delta=g["delta"], gamma=g["gamma"], iv=g["iv"],
                    oi=self.oi.get(key, 0.0))

    def eval_universe(self) -> list[dict]:
        """All streaming contracts with usable quotes + IBKR Greeks."""
        out = []
        for key in self.stream:
            c = self.contract_state(key)
            if c:
                out.append(c)
        return out

    def gamma_map(self) -> dict[tuple[float, str], float]:
        """Best-available gamma per strike for GEX: streaming Greeks first,
        wing-sweep Greeks second."""
        m: dict[tuple[float, str], float] = {}
        for key, t in self.stream.items():
            g = _greeks_from_ticker(t)
            if g and g["gamma"] and g["gamma"] > 0:
                m[key] = g["gamma"]
        for key, g in self.wing_greeks.items():
            if key not in m and g["gamma"] and g["gamma"] > 0:
                m[key] = g["gamma"]
        return m

"""BB-2C — ported from Apex run_consol_bb_2c_cycle.

Bollinger two-candle FADE on the structure timeframe (default 4h bands,
20/2.0, population sigma to match the EA):

  A = structure bar closed strictly OUTSIDE its own band
      (band computed from the 20 closes ENDING AT that bar, inclusive)
  direction: A above upper -> SELL ; A below lower -> BUY

Entry modes (BB2C_ENTRY_TRIGGER):
  "first_m1" (Apex default): no B requirement. Enter at the close of the
      FIRST M1 candle of the structure bar that opens right after A.
      Refused if that M1 closed > first_m1_max_lag_sec ago.
  "two_candle": B = next closed bar back INSIDE its own band AND in the
      correct half (>= midline after an upper A, <= midline after a lower A).
      Entry precedence:
        1. pin bar on the entry TF (default 1h) at/after B agreeing with
           direction -> entry = pin close
        2. dist(B.close, 5DMA) < 20 -> FVG limit into B's own imbalance
           (buy: gap = A.high -> B.low ; sell: gap = B.high -> A.low ;
            entry = near + (far-near)*fill_frac). No imbalance -> DROP setup.
        3. else market at B close.

  TP = the 5DMA (capped at 40 pts via _tp_capped in two_candle mode);
  SL = entry +/- 20 pts. The 5DMA must be AHEAD of entry or the setup is
  dropped (not a fade back to it otherwise).

5DMA: mean of last 5 of state["daily_closes"]; fallback SMA(5) on the
structure bars (as in Apex).

One signal per B bar (two_candle) / per forming structure bar (first_m1);
cooldown 300 s.

Interface:
  bar   : newest CLOSED structure bar dict
  state : {"bars":        [...closed structure bars, oldest->newest...],
           "entry_bars":  [...closed entry-TF bars...]      (two_candle pin),
           "bars_m1":     [...closed M1 bars...]            (first_m1),
           "daily_closes": [float, ...]  (daily closes, oldest->newest),
           "now_et": datetime (ET), "last_price": float,
           "struct_tf": "4h"  (only needed for first_m1 forming-bar math)}
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

from .signal import (Signal, bollinger_at, closes, globex_only_ok, pinbar,
                     tf_seconds, tp_capped, ts)

log = logging.getLogger("algo_es.strategies.bb2c")


def _imbalance(a_high: float, a_low: float, b_high: float, b_low: float,
               direction: str) -> Optional[Tuple[float, float]]:
    """Port of Apex _bb2c_imbalance. (near, far) with `near` the edge price
    reaches FIRST on the retrace, or None when candles overlap."""
    if direction == "buy":
        if b_low > a_high:
            return (b_low, a_high)
        return None
    if a_low > b_high:
        return (b_high, a_low)
    return None


def _find_entry_pin(entry_bars: List[Dict], after_ts: float, direction: str,
                    lookahead: int, tail_pct: float, body_pct: float,
                    nose_pct: float) -> Optional[float]:
    """Port of Apex _bb2c_find_entry_pin: first CLOSED entry-tf pin bar
    agreeing with direction that closed at/after after_ts. Forward-only;
    scans at most `lookahead` eligible bars."""
    if not entry_bars or len(entry_bars) < 2:
        return None
    n = len(entry_bars)
    want = "bull" if direction == "buy" else "bear"
    scanned = 0
    for i in range(n - 2, max(n - 2 - lookahead * 3, -1), -1):
        row = entry_bars[i]
        rts = ts(row.get("time"))
        if rts < after_ts:
            break
        scanned += 1
        if scanned > lookahead:
            break
        kind, _ = pinbar(float(row["open"]), float(row["high"]),
                         float(row["low"]), float(row["close"]),
                         tail_pct, body_pct, nose_pct)
        if kind == want:
            return float(row["close"])
    return None


class Strategy:
    def __init__(self, config: Optional[Dict] = None):
        c = config or {}
        g = c.get
        self.struct_tf = str(g("struct_tf", "4h"))
        self.entry_tf = str(g("entry_tf", "1h"))
        self.entry_trigger = str(g("entry_trigger", "first_m1")).lower()
        self.period = int(g("period", 20))
        self.deviation = float(g("deviation", 2.0))
        self.fvg_dist_pts = float(g("fvg_dist_pts", 20.0))
        self.fvg_fill_frac = float(g("fvg_fill_frac", 0.5))
        self.sl_pts = float(g("sl_pts", 20.0))
        self.tp_cap_pts = float(g("tp_cap_pts", 40.0))
        self.cooldown_sec = float(g("cooldown_sec", 300.0))
        self.pin_lookahead = int(g("pin_lookahead", 4))
        self.pin_tail_pct = float(g("pin_tail_pct", 60.0))
        self.pin_body_pct = float(g("pin_body_pct", 35.0))
        self.pin_nose_pct = float(g("pin_nose_pct", 20.0))
        self.first_m1_max_lag_sec = float(g("first_m1_max_lag_sec", 180.0))
        self.globex_only = bool(g("globex_only", True))
        self.rth_start = str(g("rth_start", "09:30"))
        self.rth_end = str(g("rth_end", "13:00"))
        self._last_bar_ts = 0.0
        self._last_fire = 0.0

    # -- 5DMA -------------------------------------------------------------
    def _dma5(self, state: Dict, struct_bars: List[Dict]) -> Optional[float]:
        dc = state.get("daily_closes") or []
        if len(dc) >= 5:
            return sum(float(x) for x in dc[-5:]) / 5.0
        cl = closes(struct_bars)
        if len(cl) >= 5:
            return sum(cl[-5:]) / 5.0       # Apex fallback: SMA(5) on struct bars
        return None

    # -- main -------------------------------------------------------------
    def on_bar(self, bar: Dict, state: Optional[Dict] = None) -> Optional[Signal]:
        state = state or {}
        if self.globex_only and not globex_only_ok(
                state, self.rth_start, self.rth_end):
            return None

        bars: List[Dict] = state.get("bars") or []
        if len(bars) < self.period + 3:
            return None
        cl = closes(bars)

        now = time.time()
        if (now - self._last_fire) < self.cooldown_sec:
            return None

        if self.entry_trigger == "first_m1":
            return self._first_m1(bars, cl, bar, state, now)
        return self._two_candle(bars, cl, state, now)

    # -- first_m1 mode ------------------------------------------------------
    def _first_m1(self, bars, cl, bar, state, now) -> Optional[Signal]:
        ia = len(bars) - 1                      # A: last CLOSED structure bar
        A = bars[ia]
        cA = float(A["close"])
        band = bollinger_at(cl, ia, self.period, self.deviation)
        if not band:
            return None
        upA, _midA, loA = band
        if cA > upA:
            direction, side = "sell", "upper"
        elif cA < loA:
            direction, side = "buy", "lower"
        else:
            return None

        # forming structure bar opens right after A
        forming_ts = ts(A.get("time")) + tf_seconds(self.struct_tf)
        if forming_ts > 0 and forming_ts == self._last_bar_ts:
            return None

        m1: List[Dict] = state.get("bars_m1") or []
        if len(m1) < 2:
            return None
        first_i = None
        for i, m in enumerate(m1):
            t = ts(m.get("time"))
            if forming_ts <= t < forming_ts + 60.0:
                first_i = i
                break
        if first_i is None or first_i >= len(m1) - 1:
            return None                          # not in feed / still forming
        entry = float(m1[first_i]["close"])
        lag = now - (ts(m1[first_i].get("time")) + 60.0)
        if lag > self.first_m1_max_lag_sec:
            self._last_bar_ts = forming_ts
            return None

        sma5 = self._dma5(state, bars)
        if not sma5 or sma5 <= 0:
            return None
        tp_px = float(sma5)
        sl_px = entry - self.sl_pts if direction == "buy" \
            else entry + self.sl_pts
        if (direction == "buy" and tp_px <= entry) or \
           (direction == "sell" and tp_px >= entry):
            self._last_bar_ts = forming_ts
            return None

        self._last_bar_ts = forming_ts
        self._last_fire = now
        name = "BB-2C-L" if direction == "buy" else "BB-2C-S"
        log.info("[BB2C] %s FIRST_M1 | A(%s) c=%.2f band=[%.2f..%.2f] | "
                 "entry=%.2f (lag %.0fs) | 5DMA=%.2f | sl=%.2f tp=%.2f",
                 direction.upper(), side, cA, loA, upA, entry, lag,
                 sma5, sl_px, tp_px)
        return Signal(
            side=direction, entry_px=entry, stop_px=sl_px, target_px=tp_px,
            strategy_name=name, confidence=0.97,
            reason=f"BB2C {side}-band fade on {self.struct_tf}: A closed "
                   f"outside, first-M1 entry",
            extra={"entry_kind": "FIRST_M1", "band_side": side,
                   "dma5": round(sma5, 2)})

    # -- two_candle mode ----------------------------------------------------
    def _two_candle(self, bars, cl, state, now) -> Optional[Signal]:
        ia, ib = len(bars) - 3, len(bars) - 2   # A, B: both CLOSED
        A, B = bars[ia], bars[ib]
        cA, cB = float(A["close"]), float(B["close"])
        hA, lA = float(A["high"]), float(A["low"])
        hB, lB = float(B["high"]), float(B["low"])

        bar_ts = ts(B.get("time"))
        if bar_ts > 0 and bar_ts == self._last_bar_ts:
            return None

        bandA = bollinger_at(cl, ia, self.period, self.deviation)
        bandB = bollinger_at(cl, ib, self.period, self.deviation)
        if not bandA or not bandB:
            return None
        upA, _midA, loA = bandA
        upB, midB, loB = bandB

        if cA > upA:
            direction, side = "sell", "upper"
        elif cA < loA:
            direction, side = "buy", "lower"
        else:
            return None                          # A not outside: no event
        if not (loB <= cB <= upB):
            return None                          # B not back inside
        if side == "upper" and cB < midB:
            return None                          # B already past midline
        if side == "lower" and cB > midB:
            return None

        sma5 = self._dma5(state, bars)
        if not sma5 or sma5 <= 0:
            return None
        dist = abs(cB - float(sma5))

        entry_bars: List[Dict] = state.get("entry_bars") or []
        pin_px = _find_entry_pin(entry_bars, bar_ts, direction,
                                 self.pin_lookahead, self.pin_tail_pct,
                                 self.pin_body_pct, self.pin_nose_pct)
        entry_kind, gap = None, None
        if pin_px is not None:
            entry = float(pin_px)
            entry_kind = f"PIN_{self.entry_tf.upper()}_CLOSE"
        else:
            gap = _imbalance(hA, lA, hB, lB, direction)
            if dist < self.fvg_dist_pts:
                if gap is None:
                    self._last_bar_ts = bar_ts   # dropped, not downgraded
                    return None
                near, far = gap
                entry = near + (far - near) * self.fvg_fill_frac
                entry_kind = "FVG_LIMIT"
            else:
                entry = cB
                entry_kind = "MARKET_B_CLOSE"

        tp_px = tp_capped(entry, float(sma5), direction, self.tp_cap_pts)
        sl_px = entry - self.sl_pts if direction == "buy" \
            else entry + self.sl_pts
        if (direction == "buy" and tp_px <= entry) or \
           (direction == "sell" and tp_px >= entry):
            self._last_bar_ts = bar_ts
            return None

        self._last_bar_ts = bar_ts
        self._last_fire = now
        name = "BB-2C-L" if direction == "buy" else "BB-2C-S"
        log.info("[BB2C] %s %s | A(%s) c=%.2f | B c=%.2f mid=%.2f | "
                 "5DMA=%.2f dist=%.1f | entry=%.2f sl=%.2f tp=%.2f",
                 direction.upper(), entry_kind, side, cA, cB, midB,
                 sma5, dist, entry, sl_px, tp_px)
        return Signal(
            side=direction, entry_px=entry, stop_px=sl_px, target_px=tp_px,
            strategy_name=name, confidence=0.97,
            reason=f"BB2C {side}-band fade on {self.struct_tf}: A closed "
                   f"outside, B back inside correct half; B->5DMA "
                   f"{dist:.1f}pts -> {entry_kind}",
            extra={"entry_kind": entry_kind, "band_side": side,
                   "dist_to_5dma": round(dist, 2)})

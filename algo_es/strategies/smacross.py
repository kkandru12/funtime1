"""SMA-CROSS — ported from Apex run_sma_cross_cycle.

ES M15 SMA(50)/SMA(200) Golden Cross / Death Cross.

  Cross confirmed on two CLOSED candles: fast[-2] vs slow[-2] ("previous")
  and fast[-1] vs slow[-1] ("current"). In Apex these are iloc[-3]/iloc[-2]
  because iloc[-1] is the forming bar; here the caller feeds CLOSED bars
  only, so the indices shift by one — the semantics are identical.

  golden: fast_prev <= slow_prev and fast_now > slow_now -> BUY
  death : fast_prev >= slow_prev and fast_now < slow_now -> SELL

Gates (in order):
  1. Globex-only (outside 09:30-13:00 ET)
  2. Session: not in the 16:59-18:00 ET maintenance pause
  3. M15 bars available (>= 205)
  4. Cross present
  5. Separation filter: |fast - slow| >= 2.0 pts (flat-zone whipsaw guard)
  6. State machine: no re-entry in the same direction (flat->long->short)
  7. Cooldown 900 s
  8. SL = +/-25 pts ; TP = GEX call/put wall when beyond +/-5 pts of entry,
     else +/-60 pts ; R:R >= 1.5 or the entry is rejected

Interface:
  bar   : newest CLOSED M15 bar dict
  state : {"bars": [...closed M15 bars, oldest->newest...],
           "now_et": datetime (ET), "last_price": float,
           "tick_size": float,
           "gex": {"call_wall": float, "put_wall": float} (optional)}
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from .signal import (Signal, apex_entry_window_open, closes, globex_only_ok,
                     round_to_tick)

log = logging.getLogger("algo_es.strategies.smacross")


class Strategy:
    def __init__(self, config: Optional[Dict] = None):
        c = config or {}
        g = c.get
        self.fast = int(g("fast", 50))
        self.slow = int(g("slow", 200))
        self.min_history = int(g("min_history", self.slow + 5))
        self.sl_points = float(g("sl_points", 25.0))
        self.tp_points = float(g("tp_points", 60.0))
        self.min_sep_pts = float(g("min_sep_pts", 2.0))
        self.cooldown_sec = float(g("cooldown_sec", 900.0))
        self.min_rr = float(g("min_rr", 1.5))
        self.tick_size = float(g("tick_size", 0.25))
        self.globex_only = bool(g("globex_only", True))
        self.rth_start = str(g("rth_start", "09:30"))
        self.rth_end = str(g("rth_end", "13:00"))
        self._state = "flat"          # flat | long | short
        self._last_fire = 0.0

    @staticmethod
    def _sma(values: List[float], n: int, end: int) -> Optional[float]:
        """SMA(n) ending at index `end` inclusive."""
        lo = end - n + 1
        if lo < 0:
            return None
        return sum(values[lo:end + 1]) / n

    def on_bar(self, bar: Dict, state: Optional[Dict] = None) -> Optional[Signal]:
        state = state or {}
        if self.globex_only and not globex_only_ok(
                state, self.rth_start, self.rth_end):
            return None
        if not apex_entry_window_open(state):
            return None

        bars: List[Dict] = state.get("bars") or []
        if len(bars) < self.min_history:
            return None

        last_price = float(state.get("last_price") or bar.get("close") or 0.0)
        if not last_price:
            return None

        cl = closes(bars)
        n = len(cl) - 1                       # last CLOSED bar
        fast_now = self._sma(cl, self.fast, n)
        fast_prev = self._sma(cl, self.fast, n - 1)
        slow_now = self._sma(cl, self.slow, n)
        slow_prev = self._sma(cl, self.slow, n - 1)
        if None in (fast_now, fast_prev, slow_now, slow_prev):
            return None

        golden = (fast_prev <= slow_prev) and (fast_now > slow_now)
        death = (fast_prev >= slow_prev) and (fast_now < slow_now)
        if not (golden or death):
            return None

        separation = abs(fast_now - slow_now)
        if separation < self.min_sep_pts:
            return None

        action = "buy" if golden else "sell"
        new_state = "long" if golden else "short"
        if self._state == new_state:
            return None                       # no same-direction re-entry

        now = time.time()
        if (now - self._last_fire) < self.cooldown_sec:
            return None

        tick = self.tick_size
        gex: Optional[Dict] = state.get("gex") or {}
        cross_label = "GOLDEN (50>200)" if golden else "DEATH (50<200)"
        if golden:
            sl_px = round_to_tick(last_price - self.sl_points, tick)
            cw = gex.get("call_wall")
            tp_px = round_to_tick(
                float(cw) if (cw and float(cw) > last_price + 5.0)
                else last_price + self.tp_points, tick)
            name = "SMA50_200_Golden_Cross"
        else:
            sl_px = round_to_tick(last_price + self.sl_points, tick)
            pw = gex.get("put_wall")
            tp_px = round_to_tick(
                float(pw) if (pw and float(pw) < last_price - 5.0)
                else last_price - self.tp_points, tick)
            name = "SMA50_200_Death_Cross"

        risk = abs(last_price - sl_px)
        reward = abs(last_price - tp_px)
        rr = reward / risk if risk > 0 else 0.0
        if rr < self.min_rr:
            return None

        self._state = new_state
        self._last_fire = now
        log.info("[SMA-CROSS] %s FIRED %s @ %.2f | SMA50=%.2f SMA200=%.2f "
                 "sep=%.2f | SL=%.2f TP=%.2f R:R=%.2f",
                 cross_label, action.upper(), last_price,
                 fast_now, slow_now, separation, sl_px, tp_px, rr)
        return Signal(
            side=action, entry_px=last_price, stop_px=sl_px, target_px=tp_px,
            strategy_name=name, confidence=0.90,
            reason=(f"M15 {cross_label} confirmed | SMA50={fast_now:.2f} "
                    f"SMA200={slow_now:.2f} sep={separation:.2f}pts | "
                    f"entry={last_price:.2f} sl={sl_px:.2f} tp={tp_px:.2f} "
                    f"R:R={rr:.2f}"),
            extra={"sma_fast": round(fast_now, 2),
                   "sma_slow": round(slow_now, 2),
                   "separation": round(separation, 2), "rr": round(rr, 2)})

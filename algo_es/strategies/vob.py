"""VOB_Retest — ported from Apex run_vob_cycle.

Pattern: Touch -> Clear -> Retest -> Execute, on closed M1 bars against
H1 EMA-cross zones (port of the MQ5 VOB_Calc).

Zone construction (port of Apex _compute_vob_zones, sensitivity=5):
  EMA(5)/EMA(18) bullish cross -> BULL zone:
      anchor = bar with the lowest low over the prior 18 bars
      zone = [anchor low, min(anchor open, anchor close)]
  EMA(5)/EMA(18) bearish cross -> BEAR zone:
      anchor = bar with the highest high over the prior 18 bars
      zone = [max(anchor open, anchor close), anchor high]
  Invalidation: bull zone dropped if last H1 close < zone lower;
                bear zone dropped if last H1 close > zone upper.
  Only the most recent zone of each side is traded (deque maxlen=1).

Per closed M1 bar (deduped by bar time), for each side in range of price:
  IDLE     + close inside wall +/- buf            -> TOUCHED
  TOUCHED  + close clears wall by atr*0.1         -> REVERSED ("CLEARED")
  REVERSED + close back inside wall +/- buf       -> FIRE

  buf  = atr(14) * 0.5 ; proximity filter = atr * 2.0
  buy:  TP = last_price + 40, SL = bull_upper - 10
  sell: TP = last_price - 40, SL = bear_lower + 10

Interface:
  bar   : newest CLOSED M1 bar dict
  state : {"bars_h1": [...closed H1 bars...],
           "bars_m1": [...closed M1 bars, for ATR history...],
           "now_et": datetime (ET), "last_price": float,
           "tick_size": float}
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from .signal import (Signal, closes, compute_atr, ema, globex_only_ok, now_et,
                     round_to_tick, ts)

log = logging.getLogger("algo_es.strategies.vob")

S_IDLE, S_TOUCHED, S_REVERSED = "IDLE", "TOUCHED", "REVERSED"


def compute_vob_zones(bars_h1: List[Dict], sensitivity: int = 5):
    """Port of Apex _compute_vob_zones. Returns (bull_zones, bear_zones),
    lists of dicts {upper, lower, mid}; caller keeps the last of each."""
    if len(bars_h1) < sensitivity + 20:
        return [], []
    l1, l2 = sensitivity, sensitivity + 13
    cl = closes(bars_h1)
    e1, e2 = ema(cl, l1), ema(cl, l2)
    bull_zones, bear_zones = [], []
    for i in range(1, len(bars_h1)):
        # bullish cross (cup)
        if e1[i] > e2[i] and e1[i - 1] <= e2[i - 1]:
            look = bars_h1[max(0, i - l2):i]
            anchor = min(look, key=lambda b: float(b["low"]))
            low_val = float(anchor["low"])
            upper = min(float(anchor["open"]), float(anchor["close"]))
            bull_zones.append({"upper": upper, "lower": low_val,
                               "mid": (upper + low_val) / 2})
        # bearish cross
        if e1[i] < e2[i] and e1[i - 1] >= e2[i - 1]:
            look = bars_h1[max(0, i - l2):i]
            anchor = max(look, key=lambda b: float(b["high"]))
            high_val = float(anchor["high"])
            lower = max(float(anchor["open"]), float(anchor["close"]))
            bear_zones.append({"upper": high_val, "lower": lower,
                               "mid": (high_val + lower) / 2})
    last_close = float(bars_h1[-1]["close"])
    bull_zones = [z for z in bull_zones if last_close >= z["lower"]][-15:]
    bear_zones = [z for z in bear_zones if last_close <= z["upper"]][-15:]
    return bull_zones, bear_zones


class Strategy:
    """VOB Touch -> Clear -> Retest."""

    def __init__(self, config: Optional[Dict] = None):
        c = config or {}
        g = c.get
        self.sensitivity = int(g("sensitivity", 5))
        self.atr_period = int(g("atr_period", 14))
        self.buf_mult = float(g("buf_mult", 0.5))        # zone half-width = atr*buf
        self.prox_mult = float(g("prox_mult", 2.0))      # proximity filter
        self.clear_mult = float(g("clear_mult", 0.1))    # clear distance = atr*0.1
        self.tp_points = float(g("tp_points", 40.0))
        self.sl_points = float(g("sl_points", 10.0))
        self.tick_size = float(g("tick_size", 0.25))
        self.zone_refresh_sec = float(g("zone_refresh_sec", 30.0))
        self.globex_only = bool(g("globex_only", True))
        self.rth_start = str(g("rth_start", "09:30"))
        self.rth_end = str(g("rth_end", "13:00"))
        # one VOB position at a time is an EXECUTOR concern (caller); the
        # state machine still resets to IDLE after firing, as in Apex.
        self._sm = {"VOB_BULL": {"state": S_IDLE},
                    "VOB_BEAR": {"state": S_IDLE}}
        self._last_bar_t = 0.0
        self._zones_t = 0.0
        self._bull_zone: Optional[Dict] = None
        self._bear_zone: Optional[Dict] = None

    # -- zones ------------------------------------------------------------
    def _refresh_zones(self, bars_h1: List[Dict]):
        now = time.time()
        if now - self._zones_t < self.zone_refresh_sec:
            return
        self._zones_t = now
        try:
            bull, bear = compute_vob_zones(bars_h1, self.sensitivity)
        except Exception as e:
            log.debug("VOB zone compute failed: %s", e)
            return
        # Apex: H1 only by default (M1 fallback behind VOB_M1_ZONE_FALLBACK=0)
        self._bull_zone = bull[-1] if bull else None
        self._bear_zone = bear[-1] if bear else None

    # -- main -------------------------------------------------------------
    def on_bar(self, bar: Dict, state: Optional[Dict] = None) -> Optional[Signal]:
        state = state or {}
        if self.globex_only and not globex_only_ok(
                state, self.rth_start, self.rth_end):
            return None

        bars_m1: List[Dict] = state.get("bars_m1") or []
        bars_h1: List[Dict] = state.get("bars_h1") or []
        last_price = float(state.get("last_price")
                           or bar.get("close") or 0.0)
        if not last_price:
            return None

        bar_t = ts(bar.get("time"))
        if bar_t and bar_t == self._last_bar_t:
            return None                      # one evaluation per closed bar
        self._last_bar_t = bar_t

        if len(bars_m1) < self.atr_period + 1 or not bars_h1:
            return None
        self._refresh_zones(bars_h1)

        atr = compute_atr(bars_m1, self.atr_period)
        if atr <= 0:
            return None
        buf, prox = atr * self.buf_mult, atr * self.prox_mult
        close_px = float(bar["close"])

        cands = []
        if self._bull_zone and \
                self._bull_zone["lower"] - prox <= close_px <= self._bull_zone["upper"] + prox:
            cands.append(("VOB_BULL", self._bull_zone["upper"], "buy"))
        if self._bear_zone and \
                self._bear_zone["lower"] - prox <= close_px <= self._bear_zone["upper"] + prox:
            cands.append(("VOB_BEAR", self._bear_zone["lower"], "sell"))

        for name, wall_px, action in cands:
            st = self._sm[name]
            cur = st["state"]
            in_zone = (wall_px - buf) <= close_px <= (wall_px + buf)
            cleared = (close_px > wall_px + atr * self.clear_mult) \
                if action == "buy" else \
                (close_px < wall_px - atr * self.clear_mult)

            if cur == S_IDLE and in_zone:
                st["state"] = S_TOUCHED
                st["touch_price"] = wall_px
                log.info("[VOB] %s TOUCHED on M1 close %.2f (zone %.2f)",
                         name, close_px, wall_px)
            elif cur == S_TOUCHED and cleared:
                st["state"] = S_REVERSED
                log.info("[VOB] %s CLEARED on M1 close %.2f — watching for retest",
                         name, close_px)
            elif cur == S_REVERSED and in_zone:
                tsign = 1.0 if action == "buy" else -1.0
                tp_px = round_to_tick(last_price + tsign * self.tp_points,
                                      self.tick_size)
                sl_px = round_to_tick(wall_px - tsign * self.sl_points,
                                      self.tick_size)
                st["state"] = S_IDLE     # Apex _wl_reset on fire
                log.info("[VOB] %s RETEST on M1 close %.2f — firing %s",
                         name, close_px, action.upper())
                return Signal(
                    side=action, entry_px=last_price,
                    stop_px=sl_px, target_px=tp_px,
                    strategy_name="VOB_Retest", confidence=0.95,
                    reason=f"Pattern Retest Confirmed @ {name}",
                    extra={"wall_px": wall_px, "atr": round(atr, 2)})
        return None

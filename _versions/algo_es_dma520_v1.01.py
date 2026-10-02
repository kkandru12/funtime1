"""DMA-520 — ported from Apex run_5_20dma_sweep ("5/20 DMA DOUBLE SWEEP").

Setup on one candle of the scan timeframe (default 1h and 4h, first match
wins). Both MAs are DAILY SMAs from confirmed daily closes.

  "inside" mode (Apex default):
      candle low  <= BOTH daily SMAs   (swept under the channel)
      candle high >= BOTH daily SMAs   (swept over it)
      close BETWEEN them               (settled back INSIDE)
  Direction is inferred, not stated by the rule: the side with the LARGER
  overshoot beyond the channel is the rejected side -> fade it.
      under = lo_line - low ; over = high - hi_line
      buy  if under >= over else sell
  Both overshoots must be >= min_overshoot_pts (2.0) or the poke is not a
  sweep.

  "outside" mode: v1.29 traverse rule (through one line, close past the
  other) WITH the pin-bar gate.

Entry modes:
  "m1" (Apex default): read the sweep off the FORMING tf candle's high/low;
      the decision close is the last CLOSED M1 (must not predate the tf
      candle, max lag 180 s).
  "closed": the last closed tf candle.

  SL = swept extreme -/+ sl_buf_pts (3.0): the wick IS the invalidation.
  TP = the tf Bollinger band (upper for buy / lower for sell, 20/2.0),
       pulled in to tp_cap_pts (40.0) via _tp_capped. Dropped when the band
       is not beyond entry.

Cooldown 300 s; one signal per tf candle; first matching tf wins.

Interface:
  bar   : newest CLOSED bar of the first scan tf (bookkeeping only)
  state : {"tf_bars": {"1h": [...], "4h": [...]},   # closed tf bars;
                                                 # include the FORMING bar
                                                 # as the last element for
                                                 # m1 entry mode
           "bars_m1": [...closed M1 bars...],
           "daily_closes": [float, ...]  (need >= slow),
           "now_et": datetime (ET)}
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

from .signal import (Signal, bollinger_at, closes, globex_only_ok, pinbar_ok,
                     ts)

log = logging.getLogger("algo_es.strategies.dma520")


class Strategy:
    def __init__(self, config: Optional[Dict] = None):
        c = config or {}
        g = c.get
        self.tfs = list(g("tfs", ["1h", "4h"]))
        self.fast = int(g("fast", 5))
        self.slow = int(g("slow", 20))
        self.close_mode = str(g("close_mode", "inside")).lower()
        self.min_overshoot_pts = float(g("min_overshoot_pts", 2.0))
        self.entry_mode = str(g("entry_mode", "m1")).lower()
        self.m1_max_lag_sec = float(g("m1_max_lag_sec", 180.0))
        self.pin_gate = bool(g("pin_gate", True))
        self.pin_tail_pct = float(g("pin_tail_pct", 60.0))
        self.pin_body_pct = float(g("pin_body_pct", 35.0))
        self.pin_nose_pct = float(g("pin_nose_pct", 20.0))
        self.sl_buf_pts = float(g("sl_buf_pts", 3.0))
        self.tp_cap_pts = float(g("tp_cap_pts", 40.0))
        self.cooldown_sec = float(g("cooldown_sec", 300.0))
        self.bb_period = int(g("bb_period", 20))
        self.bb_dev = float(g("bb_dev", 2.0))
        self.globex_only = bool(g("globex_only", True))
        self.rth_start = str(g("rth_start", "09:30"))
        self.rth_end = str(g("rth_end", "13:00"))
        self._last_bar_ts: Dict[str, float] = {}
        self._last_fire = 0.0

    # -- daily SMAs ---------------------------------------------------------
    def _dma_values(self, daily_closes: List[float]
                    ) -> Tuple[Optional[float], Optional[float]]:
        if len(daily_closes) < self.slow:
            return None, None
        dc = [float(x) for x in daily_closes]
        return (sum(dc[-self.fast:]) / self.fast,
                sum(dc[-self.slow:]) / self.slow)

    # -- one timeframe --------------------------------------------------------
    def _try_tf(self, tf: str, state: Dict, dma5: float, dma20: float,
                now: float) -> Optional[Signal]:
        tf_bars: List[Dict] = (state.get("tf_bars") or {}).get(tf) or []
        if len(tf_bars) < 3:
            return None
        m1_mode = (self.entry_mode == "m1")

        if m1_mode:
            C = tf_bars[-1]                       # forming tf candle
        else:
            C = tf_bars[-2]                       # last closed tf candle
        c_high, c_low = float(C["high"]), float(C["low"])
        c_open = float(C["open"])
        c_close = float(C["close"])
        bar_ts = ts(C.get("time"))

        if m1_mode:
            m1: List[Dict] = state.get("bars_m1") or []
            if len(m1) < 2:
                return None
            m = m1[-1]                            # last CLOSED M1
            c_close = float(m["close"])
            m_ts = ts(m.get("time"))
            if m_ts < bar_ts:
                return None                       # M1 predates this candle
            if now - (m_ts + 60.0) > self.m1_max_lag_sec:
                return None                       # stale minute

        if bar_ts > 0 and bar_ts == self._last_bar_ts.get(tf, 0.0):
            return None
        self._last_bar_ts[tf] = bar_ts

        lo_line, hi_line = min(dma5, dma20), max(dma5, dma20)
        swept_both = (c_low <= lo_line and c_high >= hi_line)

        if self.close_mode == "inside":
            if m1_mode and not swept_both:
                return None
            if not (swept_both and lo_line <= c_close <= hi_line):
                return None
            under, over = lo_line - c_low, c_high - hi_line
            direction = "buy" if under >= over else "sell"
            swept = c_low if direction == "buy" else c_high
        else:  # "outside": traverse rule + pin gate
            if c_low <= lo_line and c_close > hi_line:
                direction, swept = "buy", c_low
            elif c_high >= hi_line and c_close < lo_line:
                direction, swept = "sell", c_high
            else:
                return None
            under, over = lo_line - c_low, c_high - hi_line
            if self.pin_gate and not pinbar_ok(
                    direction, c_open, c_high, c_low, c_close,
                    self.pin_tail_pct, self.pin_body_pct, self.pin_nose_pct):
                return None

        if min(under, over) < self.min_overshoot_pts:
            return None

        cl = closes(tf_bars)
        # band at the decision candle: forming index in m1 mode, else last closed
        bidx = len(tf_bars) - 1 if m1_mode else len(tf_bars) - 2
        band = bollinger_at(cl, bidx, self.bb_period, self.bb_dev)
        if not band:
            return None
        up, _mid, low = band
        raw_tp = up if direction == "buy" else low

        entry = c_close
        sl_px = swept - self.sl_buf_pts if direction == "buy" \
            else swept + self.sl_buf_pts
        from .signal import tp_capped
        tp_px = tp_capped(entry, raw_tp, direction, self.tp_cap_pts)
        if (direction == "buy" and tp_px <= entry) or \
           (direction == "sell" and tp_px >= entry):
            return None

        risk = abs(entry - sl_px)
        rr = abs(tp_px - entry) / risk if risk > 0 else 0.0
        name = "DMA-520-L" if direction == "buy" else "DMA-520-S"
        mode = self.close_mode + ("/M1" if m1_mode else "")
        log.info("[5_20DMA] %s %s DOUBLE SWEEP (%s) | h=%.2f l=%.2f c=%.2f | "
                 "5DMA=%.2f 20DMA=%.2f | under=%.2f over=%.2f | "
                 "entry=%.2f sl=%.2f tp=%.2f(BB) R:R=%.2f",
                 tf, direction.upper(), mode, c_high, c_low, c_close,
                 dma5, dma20, under, over, entry, sl_px, tp_px, rr)
        self._last_fire = now
        return Signal(
            side=direction, entry_px=entry, stop_px=sl_px, target_px=tp_px,
            strategy_name=name, confidence=0.97,
            reason=f"{tf} {'forming ' if m1_mode else ''}candle swept BOTH "
                   f"5DMA {dma5:.2f} and 20DMA {dma20:.2f}; "
                   f"{'M1 close' if m1_mode else 'candle closed'} "
                   f"{self.close_mode} at {c_close:.2f}; TP = BB {tp_px:.2f}",
            extra={"dma5": round(dma5, 2), "dma20": round(dma20, 2),
                   "tf": tf, "rr": round(rr, 2),
                   "swept_extreme": round(swept, 2)})

    # -- main -----------------------------------------------------------------
    def on_bar(self, bar: Dict, state: Optional[Dict] = None) -> Optional[Signal]:
        state = state or {}
        if self.globex_only and not globex_only_ok(
                state, self.rth_start, self.rth_end):
            return None

        daily_closes = state.get("daily_closes") or []
        dma5, dma20 = self._dma_values(daily_closes)
        if dma5 is None:
            return None

        now = time.time()
        if (now - self._last_fire) < self.cooldown_sec:
            return None

        for tf in self.tfs:
            try:
                sig = self._try_tf(tf, state, dma5, dma20, now)
            except Exception as e:
                log.debug("[5_20DMA] %s failed: %s", tf, e)
                continue
            if sig:
                return sig                        # one trade per pass
        return None

#!/usr/bin/env python3
"""
5DMA + Structure — the main strategy.

The user's core edge: 5DMA confluence with price structure, traded on
ALL sessions (NY + Globex).

Setup:
  - Trend: prev close vs 5DMA (long if above, short if below)
  - Touch: price touches the 5DMA
  - Confluence: a structure level (prior 5d high/low, overnight high/low,
    round levels) within 0.1x ATR of the 5DMA
  - Entry: at the recovery bar's close (bar closes back on the trend side)
  - Stop: 0.3x ATR beyond the 5DMA
  - Target: nearest structure level beyond 1:1 R:R (within 3x ATR)
  - Hold: up to 5 days (swing — flatten-at-close kills this)

Sessions: ALL (NY + Globex). No session gate.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Signal:
    side: str          # "long" or "short"
    entry_px: float
    stop_px: float
    target_px: float
    strategy_name: str = "5DMA_STRUCT"
    reason: str = ""


@dataclass
class Bar:
    time: str
    open: float
    high: float
    low: float
    close: float


class FiveDMAStruct:
    def __init__(self,
                 atr_period: int = 14,
                 confluence_atr_mult: float = 0.1,
                 stop_atr_mult: float = 0.3,
                 max_target_atr_mult: float = 3.0,
                 max_hold_days: int = 5):
        self.atr_period = atr_period
        self.confluence_atr_mult = confluence_atr_mult
        self.stop_atr_mult = stop_atr_mult
        self.max_target_atr_mult = max_target_atr_mult
        self.max_hold_days = max_hold_days
        self._closes: list[float] = []
        self._bars: list[Bar] = []
        self._in_position = False

    def _atr(self) -> Optional[float]:
        if len(self._bars) < self.atr_period + 1:
            return None
        trs = []
        for i in range(-self.atr_period, 0):
            b = self._bars[i]
            p = self._bars[i - 1]
            trs.append(max(b.high - b.low,
                           abs(b.high - p.close),
                           abs(b.low - p.close)))
        return sum(trs) / len(trs)

    def _dma5(self) -> Optional[float]:
        if len(self._closes) < 5:
            return None
        return sum(self._closes[-5:]) / 5

    def _structure_levels(self, side: str, ref_px: float, atr: float) -> list[float]:
        """Prior 5d high/low, overnight high/low, round levels."""
        if len(self._bars) < 5:
            return []
        recent = self._bars[-5:]
        levels = [max(b.high for b in recent), min(b.low for b in recent)]
        # Round levels near price (ES-style 10pt grid)
        base = round(ref_px / 10) * 10
        for r in (base - 10, base, base + 10):
            levels.append(float(r))
        # Filter: beyond 1:1 in the trade direction, within max ATR
        out = []
        for lv in levels:
            dist = abs(lv - ref_px)
            if side == "long" and lv > ref_px and dist <= self.max_target_atr_mult * atr:
                out.append(lv)
            elif side == "short" and lv < ref_px and dist <= self.max_target_atr_mult * atr:
                out.append(lv)
        return sorted(out) if side == "long" else sorted(out, reverse=True)

    def on_bar(self, bar: Bar) -> Optional[Signal]:
        self._bars.append(bar)
        self._closes.append(bar.close)
        if self._in_position:
            return None  # one position at a time; exit managed by caller

        atr = self._atr()
        dma5 = self._dma5()
        if atr is None or dma5 is None or len(self._bars) < 3:
            return None

        prev = self._bars[-2]
        # Trend: prev close vs 5DMA
        side = "long" if prev.close > dma5 else "short" if prev.close < dma5 else None
        if side is None:
            return None

        # Touch: prev bar touched the 5DMA
        touched = prev.low <= dma5 <= prev.high
        if not touched:
            return None

        # Recovery: current bar closed back on trend side
        recovered = (bar.close > dma5) if side == "long" else (bar.close < dma5)
        if not recovered:
            return None

        # Confluence: structure within 0.1x ATR of 5DMA
        structs = self._structure_levels(side, dma5, atr)
        confluent = any(abs(s - dma5) <= self.confluence_atr_mult * atr for s in structs)
        if not confluent:
            return None

        # Entry at recovery bar close
        entry = bar.close
        stop = dma5 - self.stop_atr_mult * atr if side == "long" else dma5 + self.stop_atr_mult * atr
        risk = abs(entry - stop)

        # Target: nearest structure beyond 1:1
        target = None
        for lv in structs:
            if abs(lv - entry) >= risk:  # at least 1:1
                target = lv
                break
        if target is None:
            return None

        self._in_position = True
        return Signal(
            side=side, entry_px=entry, stop_px=stop, target_px=target,
            reason=f"5DMA {side} retest + structure confl @ {dma5:.1f}, tgt {target:.1f}",
        )

    def on_exit(self):
        self._in_position = False

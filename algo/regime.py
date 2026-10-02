"""v3 Module C: regime / day filter.

UNCALIBRATED scaffolding. Each morning we compute:
  - overnight gap % (prev daily close -> first spot)
  - premarket/first-30-min range % (from 1-min spot closes)
  - VIX level (+ day change when history is available)
  - day of week
and combine them into regime_score in [0,1].

REGIME_MODE="observe" (default): log the score, no effect on trading.
REGIME_MODE="live": score < REGIME_MIN_SCORE -> no new entries that day
(journaled); score >= REGIME_HIGH_SCORE -> 1.5x size (hard-capped at
REGIME_TRADE_CAP per trade).

The weights below are PLACEHOLDERS. Do not trust "live" until 20+ journaled
live days have calibrated them (see BUILD_NOTES.md open work).
"""
import asyncio
import logging
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("algo.regime")
ET = ZoneInfo("America/New_York")

# placeholder weights: gap 35 / range 35 / vix 30 (sum 1.0)
_W = {"gap": 0.35, "range": 0.35, "vix": 0.30}
_GAP_NORM = 1.0    # |gap| of 1% -> full gap score
_RANGE_NORM = 1.5  # 30-min range of 1.5% -> full range score
_VIX_LO, _VIX_HI = 12.0, 30.0  # vix 12 -> 0, vix 30 -> 1


class SpotCloses:
    """1-min spot closes (last spot seen in the minute = the close)."""
    def __init__(self, keep: int = 45):
        self.closes = deque(maxlen=keep)
        self._min = None
        self._last = 0.0

    def note(self, now_et: datetime, spot: float):
        minute = now_et.replace(second=0, microsecond=0)
        if self._min is None:
            self._min = minute
        if minute != self._min:
            if self._last > 0:
                self.closes.append(self._last)
            self._min = minute
        if spot and spot > 0:
            self._last = spot

    def ready(self, now_et: datetime) -> bool:
        """30 closes, or 10:05 ET with at least 10 (partial day-start)."""
        if len(self.closes) >= 30:
            return True
        t = (now_et.hour, now_et.minute)
        return t >= (10, 5) and len(self.closes) >= 10

    def range_pct(self) -> float | None:
        if len(self.closes) < 5 or not self.closes[0]:
            return None
        return (max(self.closes) - min(self.closes)) / self.closes[0] * 100.0


async def _prev_close(ib, contract) -> float | None:
    """Last completed daily close before today. None on any failure."""
    try:
        bars = await asyncio.wait_for(
            ib.reqHistoricalDataAsync(contract, "", "4 D", "1 day",
                                      "TRADES", True),
            timeout=20)
        today = datetime.now(ET).date()
        prev = [b.close for b in bars
                if b.close and b.close > 0 and b.date < today]
        return float(prev[-1]) if prev else None
    except Exception as e:
        log.warning("prev_close failed: %s", e)
        return None


async def morning_snapshot(ib, spot0: float) -> dict:
    """Gap / VIX / dow snapshot at startup. Never raises."""
    from ib_insync import Index
    f = {"prev_close": None, "gap_pct": None, "vix": None,
         "vix_chg_pct": None, "dow": datetime.now(ET).weekday(),
         "spot0": round(spot0, 2) if spot0 else None, "partial": []}
    try:
        spx = Index("SPX", "CBOE", "USD")
        await ib.qualifyContractsAsync(spx)
        pc = await _prev_close(ib, spx)
        if pc and spot0:
            f["prev_close"] = round(pc, 2)
            f["gap_pct"] = round((spot0 - pc) / pc * 100.0, 3)
        else:
            f["partial"].append("no-prev-close")
    except Exception as e:
        f["partial"].append(f"spx:{type(e).__name__}")
    try:
        vix = Index("VIX", "CBOE", "USD")
        await ib.qualifyContractsAsync(vix)
        t = ib.reqMktData(vix, "", True, False)  # snapshot: no line held
        await asyncio.sleep(3)
        px = t.marketPrice() or t.last or t.close
        ib.cancelMktData(vix)
        if px and px > 0:
            f["vix"] = round(float(px), 2)
            vpc = await _prev_close(ib, vix)
            if vpc:
                f["vix_chg_pct"] = round((px - vpc) / vpc * 100.0, 2)
        else:
            f["partial"].append("no-vix-quote")
    except Exception as e:
        f["partial"].append(f"vix:{type(e).__name__}")
    log.info("regime morning snapshot: %s", f)
    return f


def finalize(features: dict, range_pct: float | None) -> tuple[float, dict]:
    """regime_score in [0,1] + the per-feature breakdown.

    Weights are placeholders (uncalibrated). Missing features score 0 and
    are flagged in 'partial' so the journal shows what was unavailable.
    """
    partial = list(features.get("partial", []))
    gap = features.get("gap_pct")
    gap_s = min(abs(gap) / _GAP_NORM, 1.0) if gap is not None else 0.0
    if gap is None:
        partial.append("no-gap")
    range_s = min(range_pct / _RANGE_NORM, 1.0) if range_pct is not None else 0.0
    if range_pct is None:
        partial.append("no-range")
    vix = features.get("vix")
    vix_s = min(max(vix - _VIX_LO, 0.0) / (_VIX_HI - _VIX_LO), 1.0) \
        if vix is not None else 0.0
    if vix is None:
        partial.append("no-vix")
    score = _W["gap"] * gap_s + _W["range"] * range_s + _W["vix"] * vix_s
    detail = {"weights": dict(_W), "gap_s": round(gap_s, 3),
              "range_s": round(range_s, 3), "vix_s": round(vix_s, 3),
              "gap_pct": gap, "range_pct": round(range_pct, 3)
              if range_pct is not None else None,
              "vix": vix, "vix_chg_pct": features.get("vix_chg_pct"),
              "dow": features.get("dow"), "partial": partial,
              "calibrated": False}
    return round(score, 3), detail

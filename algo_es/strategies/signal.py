"""Shared Signal dataclass and indicator helpers for the ported Apex Globex strategies.

Bars are plain dicts: {"time", "open", "high", "low", "close"}.
"time" may be a datetime or an epoch float; helpers normalize it.
All bar lists are CLOSED bars, oldest -> newest (the Apex iloc[-2] "last
closed" convention collapses to bars[-1] here because the caller must only
ever feed closed bars).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass
class Signal:
    side: str            # "buy" | "sell"
    entry_px: float
    stop_px: float
    target_px: float
    strategy_name: str
    reason: str
    confidence: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- time ---
def ts(t) -> float:
    """Epoch seconds for a bar 'time' that may be datetime or number."""
    if t is None:
        return 0.0
    if isinstance(t, _dt.datetime):
        d = t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)
        return d.timestamp()
    try:
        return float(t)
    except (TypeError, ValueError):
        return 0.0


def now_et(state: Optional[Dict] = None) -> _dt.datetime:
    n = (state or {}).get("now_et")
    if isinstance(n, _dt.datetime):
        return n if n.tzinfo else n.replace(tzinfo=ET)
    return _dt.datetime.now(_dt.timezone.utc).astimezone(ET)


def globex_only_ok(state: Optional[Dict] = None,
                   rth_start: str = "09:30", rth_end: str = "13:00") -> bool:
    """Port of Apex _globex_only_ok: True when a non-wall cycle may run,
    i.e. OUTSIDE the 09:30-13:00 ET RTH window the wall cycles own."""
    try:
        sh, sm = (int(x) for x in rth_start.split(":")[:2])
        eh, em = (int(x) for x in rth_end.split(":")[:2])
    except (ValueError, TypeError):
        return True
    n = now_et(state)
    in_rth = (sh, sm) <= (n.hour, n.minute) < (eh, em)
    return not in_rth


def apex_entry_window_open(state: Optional[Dict] = None) -> bool:
    """Port of Apex _apex_entry_window_open: False only during the
    16:59-18:00 ET maintenance pause."""
    n = now_et(state).time()
    return not (_dt.time(16, 59) <= n < _dt.time(18, 0))


# ------------------------------------------------------------ indicators ---
def closes(bars: List[Dict]) -> List[float]:
    return [float(b["close"]) for b in bars]


def sma(values: List[float], length: int) -> Optional[float]:
    if len(values) < length or length <= 0:
        return None
    return sum(values[-length:]) / length


def compute_atr(bars: List[Dict], period: int = 14) -> float:
    """Port of Apex compute_atr: mean true range over last `period` bars."""
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, l = float(bars[i]["high"]), float(bars[i]["low"])
        pc = float(bars[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


def ema(values: List[float], span: int) -> List[float]:
    """EMA with adjust=False, matching pandas ewm(span, adjust=False)."""
    if not values:
        return []
    k = 2.0 / (span + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def bollinger_at(close_list: List[float], idx: int,
                 period: int = 20, dev: float = 2.0):
    """Port of Apex _bb2c_bands_at: (upper, mid, lower) for the bar at `idx`,
    computed from the `period` closes ENDING AT idx inclusive, POPULATION
    sigma (ddof=0) to match the EA. None when not enough history."""
    lo = idx - period + 1
    if lo < 0:
        return None
    win = [float(x) for x in close_list[lo:idx + 1]]
    if len(win) < period:
        return None
    mid = sum(win) / len(win)
    var = sum((x - mid) ** 2 for x in win) / len(win)
    sd = var ** 0.5
    return (mid + dev * sd, mid, mid - dev * sd)


def tp_capped(entry: float, target: float, direction: str,
              cap: float = 40.0) -> float:
    """Port of Apex _tp_capped: the NEARER of target and cap points from
    entry. Only ever pulls the target in. cap <= 0 disables."""
    if cap <= 0:
        return target
    capped = entry + cap if direction == "buy" else entry - cap
    if direction == "buy":
        return min(target, capped)
    return max(target, capped)


def round_to_tick(price: Optional[float], tick_size: float) -> Optional[float]:
    """Port of Apex _round_to_tick."""
    if price is None:
        return None
    try:
        px = float(price)
    except (TypeError, ValueError):
        return None
    t = float(tick_size or 0.0)
    if t <= 0:
        return round(px, 2)
    return round(round(px / t) * t, 5)


def pinbar(o: float, h: float, l: float, c: float,
           tail_pct: float = 60.0, body_pct: float = 35.0,
           nose_pct: float = 20.0):
    """Port of Apex _pinbar: ("bull"|"bear"|None, detail). Scale-free,
    fractions of the candle's own range."""
    rng = h - l
    if rng <= 0:
        return None, {"tail": 0.0, "body": 0.0, "nose": 0.0}
    top, bot = max(o, c), min(o, c)
    body = (top - bot) / rng * 100.0
    lower = (bot - l) / rng * 100.0
    upper = (h - top) / rng * 100.0
    if body > body_pct:
        return None, {"tail": max(lower, upper), "body": body,
                      "nose": min(lower, upper)}
    if lower >= tail_pct and upper <= nose_pct:
        return "bull", {"tail": lower, "body": body, "nose": upper}
    if upper >= tail_pct and lower <= nose_pct:
        return "bear", {"tail": upper, "body": body, "nose": lower}
    tail, nose = (lower, upper) if lower >= upper else (upper, lower)
    return None, {"tail": tail, "body": body, "nose": nose}


def pinbar_ok(direction: str, o: float, h: float, l: float, c: float,
              tail_pct: float = 60.0, body_pct: float = 35.0,
              nose_pct: float = 20.0, enabled: bool = True) -> bool:
    """Port of Apex _pinbar_ok: candle is a pin AGREEING with direction."""
    if not enabled:
        return True
    kind, _ = pinbar(o, h, l, c, tail_pct, body_pct, nose_pct)
    want = "bull" if direction == "buy" else "bear"
    return kind == want


def tf_seconds(tf: str) -> int:
    """'4h' -> 14400, '15m' -> 900, '1m' -> 60."""
    t = tf.strip().lower()
    if t.endswith("h"):
        return int(t[:-1]) * 3600
    if t.endswith("m"):
        return int(t[:-1]) * 60
    if t.endswith("d"):
        return int(t[:-1]) * 86400
    raise ValueError(f"unknown timeframe {tf!r}")

"""0DTE 10-point DEBIT SPREADS with a locked 10X  [v1.04 SPREAD10X]

KK (2026-10-01): "buy 0DTE 10 point spreads at the lowest premium like 0DTE
with same 10X or more with 10X locked".

ENTRY (CRUSH_VEHICLE=spread, the default; =naked restores single options)
  - The bridge's screens still choose the LONG leg exactly as before (10X-OTM
    crush windows A/B, or the wall-break sleeve).
  - The SHORT leg is the same expiry/right 10 points further OTM
    (call: strike+10, put: strike-10).
  - Debit = long ask - short bid (natural).  Max value = width ($10).
    Only spreads that can pay >= 10X are allowed: debit <= width / 10
    (= $1.00), and debit >= CRUSH_SPREAD_MIN_DEBIT ($0.05, no junk).
  - Of the bridge's ranked candidates, the spread with the LOWEST debit wins.
  - Size: CRUSH_FIXED_QTY (10) spreads.  Max loss = debit x 100 x 10
    (<= $1,000 per trade).

EXIT (mark = long bid - short ask, the natural price to close)
  1. 15:55 ET -> flat (same as singles).
  2. Native resting combo SELL at CRUSH_SPREAD_CAP_PCT of width (95% = $9.50)
     placed at fill -- captures the max even if this process dies.
  3. 10X LOCK: the first time mark >= 10 x debit the floor is set to 10 x
     debit and the spread runs for more; the floor ratchets up with the
     30% giveback trail but NEVER below 10 x debit.  A close at/below the
     floor for CRUSH_SPREAD_LOCK_TICKS loops (default 2 = ~10 s) sells.
  4. No stop before 10X (same as singles: the -2% NetLiq kill switch is the
     backstop).
"""
import logging
from datetime import datetime

import config

log = logging.getLogger("algo.spreads")

HOLD, TRAIL_STOP, FLATTEN, CAP_FILL = "HOLD", "TRAIL_STOP", "FLATTEN", "CAP_FILL"


def _tick05(px: float) -> float:
    return round(round(px / 0.05) * 0.05, 2)


def short_key(long_key: str, width: float) -> str | None:
    """'6720C' -> '6730C' (call: further up), '6600P' -> '6590P'."""
    if not long_key or long_key[-1] not in "CP":
        return None
    k = float(long_key[:-1])
    r = long_key[-1]
    s = k + width if r == "C" else k - width
    return f"{s:g}{r}"


def build(cand: dict, quote, width: float = None):
    """One bridge candidate -> spread dict, or (None, reason)."""
    width = width or config.SPREAD_WIDTH
    sk = short_key(cand.get("key"), width)
    if not sk:
        return None, "bad-key"
    lb, la = quote(cand["key"])
    sb, sa = quote(sk)
    if not la or la <= 0:
        return None, "no-long-ask"
    if sb is None and sa is None:
        return None, "short-leg-not-streamed"
    sb = sb or 0.0
    debit = _tick05(max(la - sb, 0.0))
    if debit < config.SPREAD_MIN_DEBIT:
        return None, "debit<min"
    if debit > width / config.SPREAD_MIN_MULT + 1e-9:
        return None, "cannot-pay-10x"
    sp = dict(cand)
    sp.update(vehicle="spread", long_key=cand["key"], short_key=sk,
              key=f"{cand['key']}/{sk}", width=width, ask=debit,
              debit=debit, max_mult=round(width / debit, 1),
              long_ask=la, short_bid=sb)
    return sp, "ok"


def pick(cands: list, quote, width: float = None):
    """Lowest-debit spread that can pay >= 10X. Returns (spread|None, rejects)."""
    good, rejects = [], {}
    for c in cands or []:
        sp, why = build(c, quote, width)
        if sp:
            good.append(sp)
        else:
            rejects[why] = rejects.get(why, 0) + 1
    if not good:
        return None, rejects
    good.sort(key=lambda s: (s["debit"], -(s.get("score") or 0)))
    return good[0], rejects


def mark(pos, quote) -> float:
    """Natural close value of a spread: long bid - short ask (>= 0)."""
    lb, _ = quote(pos.long_key)
    _, sa = quote(pos.short_key)
    if not lb:
        return 0.0
    return max(lb - (sa or 0.0), 0.0)


class SpreadPosition:
    """Long debit spread with a 10X lock. Exposes the attributes main.py's
    loop reads from Position, so the loop drives both vehicles."""
    is_spread = True
    tiered_effective = False

    def __init__(self, sp: dict, qty: int, debit: float, entry_time: str,
                 simulated: bool = True):
        self.key = sp["key"]
        self.long_key, self.short_key = sp["long_key"], sp["short_key"]
        self.strike, self.right = sp.get("strike"), sp.get("right")
        self.width = sp["width"]
        self.qty = qty
        self.runner_qty = qty
        self.entry_px = debit
        self.wall = sp.get("wall")
        self.window = sp.get("window")
        self.spot_at_entry = sp.get("spot")
        self.trigger = sp.get("trigger", "crush") + "-spread"
        self.entry_time = entry_time
        self.simulated = simulated
        self.lock_px = _tick05(debit * config.SPREAD_LOCK_MULT)
        self.cap_px = _tick05(min(self.width * config.SPREAD_CAP_PCT,
                                  self.width - 0.05))
        self.tp_px = self.cap_px
        self.state = "PRE_LOCK"
        self.trail_peak = 0.0
        self.trail_floor = self.lock_px
        self.trail_below = 0
        self.trail_arm_emitted = False
        self.tp_touched = False
        self.max_bid = debit
        self.min_bid = debit
        self.realized = 0.0

    def update(self, now_et: datetime, value: float, spot: float = None) -> str:
        if (now_et.hour, now_et.minute) >= config.FLAT_TIME:
            return FLATTEN
        if not value or value <= 0:
            return HOLD
        self.max_bid = max(self.max_bid, value)
        self.min_bid = min(self.min_bid, value)
        if self.simulated and value >= self.cap_px:
            return CAP_FILL                     # dry-run: the resting cap fills
        if self.state == "PRE_LOCK":
            if value >= self.lock_px:           # 10X reached -> lock it
                self.state = "TRAIL"
                self.tp_touched = True
                self.trail_peak = value
                self.trail_floor = max(self.lock_px,
                                       value * (1 - config.TRAIL_GIVEBACK))
                log.info("10X LOCK %s value=%.2f floor=%.2f", self.key,
                         value, self.trail_floor)
            return HOLD
        # TRAIL (locked): ratchet up, never below 10X
        if value > self.trail_peak:
            self.trail_peak = value
            self.trail_floor = max(self.lock_px,
                                   value * (1 - config.TRAIL_GIVEBACK))
        if value <= self.trail_floor:
            self.trail_below += 1
            if self.trail_below >= config.SPREAD_LOCK_TICKS:
                return TRAIL_STOP
        else:
            self.trail_below = 0
        return HOLD

    def on_close(self, qty: int, px: float) -> float:
        pnl = (px - self.entry_px) * 100 * qty
        self.realized += pnl
        return pnl

    def held_minutes(self, now_et: datetime) -> int:
        try:
            h, m = self.entry_time.split(":")
            t0 = now_et.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            return max(0, int((now_et - t0).total_seconds() // 60))
        except Exception:
            return -1

    def drain_tier_fills(self):
        return []

    @property
    def peak_multiple(self):
        return self.max_bid / self.entry_px if self.entry_px else 0

    @property
    def mfe(self):
        return (self.max_bid - self.entry_px) / self.entry_px if self.entry_px else 0

    @property
    def mae(self):
        return (self.min_bid - self.entry_px) / self.entry_px if self.entry_px else 0

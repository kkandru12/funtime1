"""Position exit state machine (v3) — consumer mode.

ENTRY screens (crush evaluate + wall-break WallBreakState) moved to
gex_bridge/screen.py, which runs where the data lives. This module keeps
the exit state machine, window helpers, and flat-time check.

EXIT (priority order) - their measured design:
  1. 15:55 -> FLAT everything
  2. resting 10x limit TP (placed AT FILL, native in live mode)
  3. on 10x touch -> 30% giveback TRAIL on remainder
     (floor ratchets: max(10x entry, peak * 0.70), ~20s confirmation)
  4. per-trade stop only if config.STOP_PCT > 0 (default 0 = disabled)
"""
import logging
from datetime import datetime

import config

log = logging.getLogger("algo.strategy")


def in_entry_window(now_et: datetime) -> bool:
    return config.in_entry_window(now_et)


def past_flat_time(now_et: datetime) -> bool:
    return (now_et.hour, now_et.minute) >= config.FLAT_TIME




# ---------------- position state machine ----------------
HOLD, T1_FILL, T2_FILL, TP_TOUCH, TRAIL_STOP, STOP, FLATTEN = \
    "HOLD", "T1_FILL", "T2_FILL", "TP_TOUCH", "TRAIL_STOP", "STOP", "FLATTEN"


class Position:
    """Long 0DTE ticket.

    v2 path (tiered off): resting 10x TP -> 30% giveback trail.
    v3 Module A (tiered on, default): T1 resting 5x on 1/3, T2 resting 10x on
    1/3, runner 1/3 on the 30% giveback trail. States:
      "PRE_T1" -> 5x touch -> "PRE_T2" -> 10x touch -> "TRAIL".
    Tier fills are recorded in pending_tier_fills for the caller to journal;
    in live mode the exchange drives fills via apply_tier_fill(), in dry-run
    update() simulates them (simulated=True).
    """

    def __init__(self, key, strike, right, qty, entry_px, wall, entry_time,
                 window, spot_at_entry, trigger="crush", tiered=None,
                 simulated=True):
        self.key = key
        self.strike = strike
        self.right = right          # "call" | "put"
        self.qty = qty              # remaining contracts (all tiers)
        self.entry_px = entry_px
        self.wall = wall
        self.entry_time = entry_time
        self.window = window
        self.spot_at_entry = spot_at_entry
        self.trigger = trigger      # "crush" | "wallbreak"
        self.simulated = simulated
        self.tiered = config.TIERED_EXITS if tiered is None else tiered
        # tier split: 1/3, 1/3, runner gets the remainder
        t1 = qty // 3 if self.tiered else 0
        t2 = qty // 3 if self.tiered else 0
        self.t1_qty = t1
        self.t2_qty = t2
        self.runner_qty = qty - t1 - t2
        # qty too small to tier (<3) -> fall back to the v2 single-TP path
        self.tiered_effective = self.tiered and self.runner_qty < qty
        if self.tiered_effective:
            self.tp1_px = round(entry_px * config.TIER1_MULT / 0.05) * 0.05
            self.tp2_px = round(entry_px * config.TIER2_MULT / 0.05) * 0.05
            self.state = "PRE_T1"
        else:
            self.tp_px = round(entry_px * config.TP_MULT / 0.05) * 0.05
            self.state = "PRE_TP"
        self.trail_peak = 0.0
        self.trail_floor = self.tp2_px if self.tiered_effective else self.tp_px
        self.trail_below = 0        # consecutive ticks below floor
        self.trail_arm_emitted = False
        self.max_bid = entry_px
        self.min_bid = entry_px
        self.realized = 0.0
        self.tp_touched = False
        self.pending_tier_fills = []  # (tier, qty, px, pnl) awaiting journal

    # -- tier bookkeeping (shared by dry-run sim and live native fills) --
    def _fill_tier(self, tier: int, px: float):
        fq = self.t1_qty if tier == 1 else self.t2_qty
        fq = max(int(fq), 0)
        if fq <= 0:
            return 0.0
        pnl = self.on_close(fq, px)
        self.qty -= fq
        if tier == 1:
            self.t1_qty = 0
            self.state = "PRE_T2"
        else:
            self.t2_qty = 0
        self.pending_tier_fills.append((tier, fq, px, pnl))
        log.info("tier %d filled: %s qty=%d @ %.2f (%.1fx)",
                 tier, self.key, fq, px, px / self.entry_px if self.entry_px else 0)
        return pnl

    def apply_tier_fill(self, tier: int, fq: int, px: float) -> float:
        """Live-mode entry point: a resting native TP partially/fully filled."""
        if tier == 1:
            take = min(int(fq), int(self.t1_qty))
            self.t1_qty -= take
        elif tier == 2:
            take = min(int(fq), int(self.t2_qty))
            self.t2_qty -= take
        else:
            take = 0
        if take <= 0:
            return 0.0
        pnl = self.on_close(take, px)
        self.qty -= take
        self.pending_tier_fills.append((tier, take, px, pnl))
        if tier == 1 and self.t1_qty == 0 and self.state == "PRE_T1":
            self.state = "PRE_T2"
        return pnl

    def drain_tier_fills(self):
        out = self.pending_tier_fills
        self.pending_tier_fills = []
        return out

    # -- trail arming (called once when 10x touches) --
    def arm_trail(self, touch_bid: float):
        self.state = "TRAIL"
        self.tp_touched = True
        tp_ref = self.tp2_px if self.tiered_effective else self.tp_px
        self.trail_peak = max(touch_bid, tp_ref)
        self.trail_floor = max(tp_ref, self.trail_peak * (1 - config.TRAIL_GIVEBACK))
        self.trail_below = 0
        log.info("trail armed: peak=%.2f floor=%.2f (10x=%.2f) runner_qty=%d",
                 self.trail_peak, self.trail_floor, tp_ref, self.runner_qty)

    def update(self, now_et: datetime, bid: float, spot: float) -> str:
        if past_flat_time(now_et):
            return FLATTEN
        if bid and bid > 0:
            self.max_bid = max(self.max_bid, bid)
            self.min_bid = min(self.min_bid, bid)

        if self.state == "TRAIL":
            if bid and bid > 0:
                if bid > self.trail_peak:
                    self.trail_peak = bid
                    tp_ref = self.tp2_px if self.tiered_effective else self.tp_px
                    self.trail_floor = max(
                        tp_ref, self.trail_peak * (1 - config.TRAIL_GIVEBACK))
                if bid <= self.trail_floor:
                    self.trail_below += 1
                    if self.trail_below >= config.TRAIL_CONFIRM_TICKS:
                        return TRAIL_STOP
                else:
                    self.trail_below = 0
            return HOLD

        if not bid or bid <= 0:
            return HOLD

        # v3 tiered dry-run: simulate resting-limit fills in price order
        if self.tiered_effective and self.simulated:
            filled = None
            if self.state == "PRE_T1" and bid >= self.tp1_px:
                self._fill_tier(1, self.tp1_px)
                filled = T1_FILL
            if self.state == "PRE_T2" and bid >= self.tp2_px:
                self._fill_tier(2, self.tp2_px)
                self.arm_trail(bid)
                filled = T2_FILL
            if filled:
                return filled
            if self.state != "TRAIL" and config.STOP_PCT > 0 and \
                    bid <= self.entry_px * (1 - config.STOP_PCT):
                return STOP
            return HOLD

        # v2 single-TP path (tiered off, or qty too small to tier)
        if not self.tiered_effective:
            if bid >= self.tp_px:
                return TP_TOUCH
            if config.STOP_PCT > 0 and bid <= self.entry_px * (1 - config.STOP_PCT):
                return STOP
            return HOLD
        # live tiered: the exchange drives tier fills via apply_tier_fill();
        # the software loop only manages stop / flatten / trail.
        if config.STOP_PCT > 0 and bid <= self.entry_px * (1 - config.STOP_PCT):
            return STOP
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

    @property
    def peak_multiple(self):
        return self.max_bid / self.entry_px if self.entry_px else 0

    @property
    def mfe(self):
        return (self.max_bid - self.entry_px) / self.entry_px if self.entry_px else 0

    @property
    def mae(self):
        return (self.min_bid - self.entry_px) / self.entry_px if self.entry_px else 0

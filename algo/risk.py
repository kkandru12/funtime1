"""Pre-trade risk checks + daily kill switch.

Accounting is our own (fills we made), not the broker's - auditable and
immune to anything else happening in the paper account.
"""
import logging
from datetime import datetime

import config

log = logging.getLogger("algo.risk")


class RiskManager:
    def __init__(self, account_value: float):
        self.account_value = account_value
        self.daily_stop_amt = account_value * config.DAILY_STOP_PCT
        self.trades_today = 0
        self.daily_pnl = 0.0
        self.killed = False
        self.date = None
        # v3 Module C: regime gate. Default permissive (observe mode);
        # main.py tightens it once the morning regime score is final.
        self.regime_ok = True
        self.size_mult = 1.0

    def set_regime(self, ok: bool, mult: float = 1.0):
        self.regime_ok = ok
        self.size_mult = mult
        log.info("regime set: ok=%s size_mult=%.2f", ok, mult)

    def new_day(self, today):
        if self.date != today:
            self.date = today
            self.trades_today = 0
            self.daily_pnl = 0.0
            self.killed = False

    def size_qty(self, ask: float) -> int:
        base = max(int(config.RISK_PER_TRADE // (ask * 100)), 1)
        if self.size_mult > 1.0:
            # regime size-up, hard-capped per trade
            cap = max(int(config.REGIME_TRADE_CAP // (ask * 100)), 1)
            return max(min(int(base * self.size_mult), cap), 1)
        return base

    def can_enter(self, now_et: datetime, has_position: bool,
                  trigger: str = "crush"):
        if self.killed:
            return False, "kill-switch"
        if has_position:
            return False, "one-position"
        if self.trades_today >= config.MAX_TRADES_PER_DAY:
            return False, "max-trades"
        if self.daily_pnl <= -self.daily_stop_amt:
            return False, "daily-stop"
        # v3: wall-break entries are gated on the SAME v2 windows as crush
        # (A 11:00-12:30, B 15:30-15:58, cutoff 15:50). No new trigger may
        # widen the trading day.
        if config.active_window(now_et) is None:
            return False, "time-gate"
        if not self.regime_ok:
            return False, "regime"
        return True, "ok"

    def register_partial(self, pnl: float):
        """Tier fills are partials of one trade: move PnL, not the count."""
        self.daily_pnl += pnl
        if self.daily_pnl <= -self.daily_stop_amt:
            self.killed = True
            log.error("KILL SWITCH: daily stop hit (%+.2f)", self.daily_pnl)

    def register_close(self, pnl: float):
        self.trades_today += 1
        self.daily_pnl += pnl
        log.info("trade closed: pnl=%+.2f day_pnl=%+.2f trades=%d",
                 pnl, self.daily_pnl, self.trades_today)
        if self.daily_pnl <= -self.daily_stop_amt:
            self.killed = True
            log.error("KILL SWITCH: daily stop hit (%+.2f)", self.daily_pnl)

    def check_kill(self, has_position: bool) -> bool:
        """True if the kill switch just tripped with an open position."""
        return self.killed and has_position

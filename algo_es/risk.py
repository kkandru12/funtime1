"""Pre-trade risk checks + daily kill switch, ES edition.

Own accounting (fills we made), not the broker's. The "day" is the Globex
trading date (config.session_date: Sun 18:00+ counts as Monday).
"""
import logging

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

    def new_day(self, today):
        if self.date != today:
            self.date = today
            self.trades_today = 0
            self.daily_pnl = 0.0
            self.killed = False
            log.info("new Globex day %s: risk reset", today)

    def can_enter(self, now_et, has_position: bool, trigger: str = "fade"):
        if self.killed:
            return False, "kill-switch"
        if has_position:
            return False, "one-position"
        if self.trades_today >= config.MAX_TRADES_PER_DAY:
            return False, "max-trades"
        if self.daily_pnl <= -self.daily_stop_amt:
            return False, "daily-stop"
        if not config.in_trading_session(now_et):
            return False, "session"
        return True, "ok"

    def register_close(self, pnl: float):
        self.trades_today += 1
        self.daily_pnl += pnl
        log.info("trade closed: pnl=%+.2f day_pnl=%+.2f trades=%d",
                 pnl, self.daily_pnl, self.trades_today)
        if self.daily_pnl <= -self.daily_stop_amt:
            self.killed = True
            log.error("KILL SWITCH: daily stop hit (%+.2f)", self.daily_pnl)

    def check_kill(self, has_position: bool) -> bool:
        return self.killed and has_position

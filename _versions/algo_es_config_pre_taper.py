"""ES futures GEX algo — all parameters in one place (CONSUMER mode).

Trades ES front futures ($50/pt) off GEX levels published by gex_bridge/
(the account's single IBKR streaming connection) via ../shared/levels.json.
This process holds ZERO IBKR lines and makes ZERO IBKR calls; execution is
via the AMP MT5 terminal on the same machine (MT5Executor, demo account).

Sleeves:
  FADE     (+gamma regime): short call wall / long put wall, target = flip
  BREAKOUT (-gamma regime or wall-break event): momentum with the break
  PA C/D   (overnight): pure price-action sleeves off MT5 M1 bars

All overrides via env vars prefixed ES_.
"""
import os as _os


def _load_dotenv():
    """[v1.01 ENVLOAD] Read KEY=VALUE lines from <repo>/.env and <component>/.env
    into os.environ. Real environment variables win (never overwritten).
    Values are never printed."""
    here = _os.path.dirname(_os.path.abspath(__file__))
    for path in (_os.path.join(here, ".env"), _os.path.join(_os.path.dirname(here), ".env")):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k and k not in _os.environ:
                        _os.environ[k] = v
        except OSError:
            pass


_load_dotenv()

import os
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _f(name, default):
    return float(os.getenv(name, default))


def _i(name, default):
    return int(os.getenv(name, default))


def _s(name, default):
    return os.getenv(name, default)


def _b(name, default):
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off", "")


# ---------------- execution backend ----------------
# MT5 only: orders go to the AMP MT5 terminal on the same machine
# (MT5Executor -> demo account). There is no IBKR path in consumer mode.
EXECUTOR = "mt5"

# ---------------- bridge (shared levels) ----------------
# All three processes (gex_bridge, algo, algo_es) must be siblings, as in
# the zips. Override with ES_SHARED_DIR if laid out differently.
SHARED_DIR = _s("ES_SHARED_DIR", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))

# ---------------- instruments ----------------
FUT_SYMBOL = _s("ES_FUT_SYMBOL", "ES")
FUT_EXCHANGE = _s("ES_FUT_EXCHANGE", "CME")
FUT_CURRENCY = _s("ES_FUT_CURRENCY", "USD")
FUT_MULT = _f("ES_FUT_MULT", 50.0)          # $50 / point
FUT_TICK = _f("ES_FUT_TICK", 0.25)         # ES tick size

# ---------------- Globex session clock (ET) ----------------
# Globex: Sun 18:00 -> Fri 17:00, daily halt 17:00-18:00 ET.
HALT_START = (17, 0)
HALT_END = (18, 0)
FLATTEN_BEFORE_HALT = _b("ES_FLATTEN_BEFORE_HALT", True)
FLATTEN_AHEAD_MIN = _i("ES_FLATTEN_AHEAD_MIN", 5)   # flatten at 16:55 ET
NO_WEEKEND_HOLD = _b("ES_NO_WEEKEND_HOLD", True)    # always flat Fri 16:55


def _t(now):
    return (now.hour, now.minute)


def in_trading_session(now_et) -> bool:
    """True when Globex is open AND not in the daily 17:00-18:00 halt."""
    wd = now_et.weekday()          # Mon=0 .. Sun=6
    t = _t(now_et)
    if wd == 5:                    # Saturday: closed
        return False
    if wd == 4 and t >= HALT_START:  # Friday 17:00 -> closed for weekend
        return False
    if wd == 6 and t < HALT_END:    # Sunday before 18:00: closed
        return False
    if HALT_START <= t < HALT_END:   # daily maintenance halt
        return False
    return True


def session_date(now_et):
    """Globex 'trading date': Sun 18:00+ belongs to Monday's session."""
    d = now_et.date()
    if now_et.weekday() == 6 and _t(now_et) >= HALT_END:
        from datetime import timedelta
        d = d + timedelta(days=1)
    return d


def next_session_start(now_et):
    """Next ET datetime when in_trading_session() is True (15-min walk)."""
    from datetime import timedelta
    t = now_et.replace(second=0, microsecond=0) + timedelta(minutes=15)
    for _ in range(4 * 24 * 4):     # up to 4 days out
        if in_trading_session(t):
            return t
        t += timedelta(minutes=15)
    return t


def past_flatten_time(now_et) -> bool:
    """Within FLATTEN_AHEAD_MIN of the 17:00 halt (default: from 16:55)."""
    mins = now_et.hour * 60 + now_et.minute
    start = 17 * 60 - FLATTEN_AHEAD_MIN
    return start <= mins < 17 * 60


# ---------------- GEX (published by the bridge; read-only here) ----------------
FADE_MIN_CONFIDENCE = _f("ES_FADE_MIN_CONFIDENCE", 0.5)       # fade needs >= this
BREAKOUT_MIN_CONFIDENCE = _f("ES_BREAKOUT_MIN_CONFIDENCE", 0.35)  # breakout arm
OVERNIGHT_CONF_BUMP = _f("ES_OVERNIGHT_CONF_BUMP", 0.15)  # +conf bar on frozen walls
LEVELS_STALE_SEC = _f("ES_LEVELS_STALE_SEC", 120)  # GEX sleeves stand down past this

# ---------------- FADE sleeve (+gamma) ----------------
FADE_ENABLED = _b("ES_FADE_ENABLED", True)
FADE_TOUCH_PTS = _f("ES_FADE_TOUCH_PTS", 3.0)   # arm when spot within this of wall
FADE_STOP_PTS = _f("ES_FADE_STOP_PTS", 6.0)     # stop beyond the wall
FADE_FILL_TIMEOUT = _f("ES_FADE_FILL_TIMEOUT", 60)  # sec to fill at wall
FADE_MAX_TOUCH_PER_WALL = _i("ES_FADE_MAX_TOUCH_PER_WALL", 2)
FADE_MIN_WALL_SEP = _f("ES_FADE_MIN_WALL_SEP", 10.0)  # wall must be this far
# from spot-side... (min wall distance from spot to bother)

# ---------------- BREAKOUT sleeve (-gamma or wall-break event) ----------------
BREAKOUT_ENABLED = _b("ES_BREAKOUT_ENABLED", True)
WALL_DOMINANCE = _f("ES_WALL_DOMINANCE", 2.5)   # wall OI >= 2.5x adjacent
BREAK_ARM_PTS = _f("ES_BREAK_ARM_PTS", 10.0)
BREAK_BUFFER_PTS = _f("ES_BREAK_BUFFER_PTS", 2.0)  # closed M1 must clear wall+buf
BREAK_CONFIRM_BARS = _i("ES_BREAK_CONFIRM_BARS", 2)
BREAK_STOP_PTS = _f("ES_BREAK_STOP_PTS", 4.0)   # back inside the wall
BREAK_MAX_PER_WALL = _i("ES_BREAK_MAX_PER_WALL", 2)

# ---------------- overnight PA sleeves (C: range fade, D: sweep+reclaim) ----------------
# Overnight GEX walls are frozen/stale, so these sleeves trade pure price
# action off MT5-built M1 bars (mt5_data.py). Active only in the true
# overnight window 18:00-09:30 ET; hard blackout 09:00-09:30 (NY handoff).
SLEEVE_C = _b("ES_SLEEVE_C", True)    # overnight range fade (mean reversion)
SLEEVE_D = _b("ES_SLEEVE_D", True)    # liquidity sweep + reclaim
PA_RANGE_MIN = _f("ES_PA_RANGE_MIN", 8.0)    # fade only if ON range in [8,30]
PA_RANGE_MAX = _f("ES_PA_RANGE_MAX", 30.0)
PA_STOP = _f("ES_PA_STOP", 4.0)              # sleeve C stop beyond the extreme
PA_TOUCH_PTS = 1.0                          # fade trigger: within this of extreme
PA_RETRACE = 0.70                           # sleeve C target: 70% retracement
SWEEP_MIN = _f("ES_SWEEP_MIN", 2.0)         # sweep = extreme exceeded by >= this
SWEEP_RECLAIM_MIN = _i("ES_SWEEP_RECLAIM_MIN", 15)  # reclaim window (minutes)
SWEEP_STOP = 3.0                            # sleeve D stop beyond sweep extreme
PA_MAX_FADES_PER_SIDE = 2
PA_MAX_SWEEPS_PER_SIDE = 1
PA_BLACKOUT_START = (9, 0)
PA_BLACKOUT_END = (9, 30)
MT5_BAR_TZ = _s("ES_MT5_BAR_TZ", "UTC")  # ZoneInfo name for MT5 bar timestamps
MT5_BAR_LOOKBACK = _i("ES_MT5_BAR_LOOKBACK", 900)  # M1 bars kept for ON range


def is_pa_overnight(now_et) -> bool:
    """True in the tradable overnight window: Globex open, 18:00-09:00 ET.
    (09:00-09:30 is the NY-handoff blackout; 09:30+ is the NY session.)"""
    if not in_trading_session(now_et):
        return False
    t = (now_et.hour, now_et.minute)
    return t >= (18, 0) or t < (9, 0)


def overnight_session_start(now_et):
    """ET datetime of the 18:00 that opened the current overnight session."""
    from datetime import timedelta
    base = now_et.replace(hour=18, minute=0, second=0, microsecond=0)
    if (now_et.hour, now_et.minute) >= (18, 0):
        return base
    return base - timedelta(days=1)

# ---------------- risk ----------------
RISK_PER_TRADE = _f("ES_RISK_PER_TRADE", 200.0)
MAX_TRADES_PER_DAY = _i("ES_MAX_TRADES_PER_DAY", 4)
DAILY_STOP_PCT = _f("ES_DAILY_STOP_PCT", 0.02)
ACCOUNT_FALLBACK = _f("ES_ACCOUNT_FALLBACK", 10000.0)

# ---------------- misc ----------------
LOOP_CADENCE_SEC = _f("ES_LOOP_CADENCE_SEC", 5)
HEARTBEAT_SEC = _i("ES_HEARTBEAT_SEC", 60)
LOG_DIR = _s("ES_LOG_DIR", "logs")


# ---------------- Globex strategy sleeve [v1.02 GLOBEXWIRE] ----------------
# The five ported Apex Globex strategies (+ 5DMA-structure) wired into the
# main loop via globex.py.  They only enter when no GEX sleeve candidate
# exists and the account is flat (one position at a time, same risk gates).
# [v1.03 FIXEDQTY] KK: every ES trade is exactly 1 contract.  >0 overrides the
# $-risk sizing in strategy.size_contracts (overnight halving keeps it at 1).
FIXED_QTY = _i("ES_FIXED_QTY", 1)

GLOBEX_ENABLED = _b("ES_GLOBEX_ENABLED", True)
GLOBEX_STRATEGIES = _s("ES_GLOBEX_STRATEGIES",
                       "vob,squeeze,bb2c,dma520,smacross,fivedma")
GLOBEX_QTY = _i("ES_GLOBEX_QTY", 1)
# [BB2C-H4] default H4 only (KK 2026-10-02 after the 21-month replay:
# H1 lost money). [BB2CDUAL] BB-2C structure timeframes, each its own instance (4h: H4 bands;
# 1h: H1 bands). "4h" alone restores the single-TF behaviour.
BB2C_TFS = [t.strip().lower() for t in _s("ES_BB2C_TFS", "4h").split(",") if t.strip() in ("4h", "1h")] or ["4h"]
# [BB2C-M1B] BB-2C entry: m1_b = first M1 close back inside the band during
# the 2nd candle (default); first_m1 = v1.00; two_candle = Apex B-close/pin/FVG.
BB2C_ENTRY = _s("ES_BB2C_ENTRY", "m1_b").lower()
# True = run around the clock (KK: Globex strategies run all the time).
# False = Apex rule: stand aside 09:30-13:00 ET (the wall cycles' window).
GLOBEX_ALL_HOURS = _b("ES_GLOBEX_ALL_HOURS", True)
GLOBEX_REFRESH_SEC = _f("ES_GLOBEX_REFRESH_SEC", 30)

# [v1.05 RESILIENT] no MT5 quote for this long -> reconnect (30s..300s backoff)
MT5_STALE_SEC = _f("ES_MT5_STALE_SEC", 60)

# [v1.07 5DMA-M1] 5DMA-STRUCT entry: "m1" = first M1 close back on the trend
# side after today touches the 5DMA (default); "daily" = next daily close (v1.06)
FIVEDMA_ENTRY = _s("ES_5DMA_ENTRY", "m1").strip().lower()

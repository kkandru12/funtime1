"""All strategy / connection / budget parameters in one place.

v2 (2026-10-01): MERGED with the user's proven 10X-OTM mechanics after a
5-day Databento showdown (~/workspace/algo-review/showdown.md). The old
13:00-15:50 crush window measured 0% 10x hit on all 5 days and is retired.
Entry windows + exits are theirs (measured: avg 9.08x vs 1.36x, max 29.67x
vs 2.83x, 10x touch 28.6% vs 0.0%); chassis (paper gate, kill switch,
$200 sizing) is ours. Overrides via env vars prefixed CRUSH_.
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


# ---------------- IBKR connection ----------------
# Target: IB Gateway on the VPS (headless, lighter than TWS).
# Gateway paper = 4002 | TWS paper = 7497 (manual backup)
IB_HOST = _s("CRUSH_IB_HOST", "127.0.0.1")
IB_PORT = _i("CRUSH_IB_PORT", 7497)  # TWS paper (user runs TWS on VPS); Gateway paper=4002, TWS live=7496
IB_CLIENT_ID = _i("CRUSH_IB_CLIENT_ID", 7)
CONNECT_TIMEOUT = _f("CRUSH_CONNECT_TIMEOUT", 20)
CONNECT_RETRIES = _i("CRUSH_CONNECT_RETRIES", 10)

# ---------------- session clock (ET) ----------------
# v2: two 10X-OTM windows. Format: (id, start, end, otm_min_pts, otm_max_pts).
# Window A: morning lottery. Window B: late rip. 13:00-15:00 measured DEAD
# (0% 10x hit, 5 days, both filters) and is deliberately not traded.
ENTRY_WINDOWS = (
    ("A", (11, 0), (12, 30), _i("CRUSH_WIN_A_OTM_MIN", 40), _i("CRUSH_WIN_A_OTM_MAX", 55)),
    ("B", (15, 30), (15, 58), _i("CRUSH_WIN_B_OTM_MIN", 8), _i("CRUSH_WIN_B_OTM_MAX", 12)),
)
ENTRY_CUTOFF = (15, 50)  # no entries at/after, both windows
FLAT_TIME = (15, 55)     # flat everything, no exceptions
LOOP_END = (16, 5)       # stop the process after this


def active_window(now_et):
    """Return (id, otm_min, otm_max) if now is inside a tradable entry window,
    else None. Entry cutoff 15:50 applies to both windows."""
    t = (now_et.hour, now_et.minute)
    if t >= ENTRY_CUTOFF:
        return None
    for wid, s, e, omin, omax in ENTRY_WINDOWS:
        if s <= t < e:
            return wid, omin, omax
    return None


def in_entry_window(now_et) -> bool:
    return active_window(now_et) is not None

# ---------------- entry filters (10X-OTM, measured) ----------------
ASK_MIN = _f("CRUSH_ASK_MIN", 0.20)      # theirs: $0.20 floor (measured band)
ASK_MAX = _f("CRUSH_ASK_MAX", 0.50)
DELTA_MAX = _f("CRUSH_DELTA_MAX", 0.15)   # abs(delta), IBKR-sent Greeks
SPREAD_MAX = _f("CRUSH_SPREAD_MAX", 0.25)  # (ask-bid)/mid; tighter than their $0.50 abs cap
ASK_Z_MAX = _f("CRUSH_ASK_Z_MAX", -1.0)   # crush BAND top: must be at least 1sd crushed
ASK_Z_MIN = _f("CRUSH_ASK_Z_MIN", -2.0)   # crush BAND floor: deeper than -2.0 measured
                                          # worse than random (dying, not mispriced)
ASK_Z_LOOKBACK_MIN = _i("CRUSH_ASK_Z_LOOKBACK_MIN", 30)
IV_SPIKE_RATIO = _f("CRUSH_IV_SPIKE_RATIO", 1.3)  # skip entries if ATM IV > 1.3x trailing median
IV_LOOKBACK_MIN = _i("CRUSH_IV_LOOKBACK_MIN", 30)
MIN_ASK_HISTORY = _i("CRUSH_MIN_ASK_HISTORY", 10)  # minutes of history before entries allowed

# ---------------- exits (10X-OTM: resting 10x TP -> 30% giveback trail) ----------------
TP_MULT = _f("CRUSH_TP_MULT", 10.0)          # resting 10x limit TP placed AT FILL
TRAIL_GIVEBACK = _f("CRUSH_TRAIL_GIVEBACK", 0.30)  # 30% giveback trail after 10x touch
TRAIL_CONFIRM_TICKS = _i("CRUSH_TRAIL_CONFIRM_TICKS", 4)  # ~20s at 5s loop cadence
STOP_PCT = _f("CRUSH_STOP_PCT", 0.0)        # per-trade stop DISABLED by default
                                            # (their measurement: removing it added
                                            #  $37,100/5 sessions). >0 re-enables it.
                                            # Daily kill switch is the backstop.

# ---------------- v3 Module A: tiered exits ----------------
# T1: 1/3 at 5x, T2: 1/3 at 10x (resting native limits at fill), runner 1/3
# on the 30% giveback trail. Off = v2 behavior (full size 10x TP + trail).
TIERED_EXITS = _b("CRUSH_TIERED_EXITS", True)
TIER1_MULT = _f("CRUSH_TIER1_MULT", 5.0)
TIER2_MULT = _f("CRUSH_TIER2_MULT", 10.0)   # == TP_MULT; separate knob for clarity

# ---------------- v3 Module B: wall-break sleeve ----------------
# Event-driven entries on dominant-wall breaks, independent of crush windows.
# Semantics ported from the user's _wb_step (WALLBRK): arm on dominant wall
# (ratio>=2.5) near spot, break on closed M1s beyond the wall. Deviation: OUR
# vehicle is outrights (not their 10-wide debit spreads).
WALLBREAK_ENABLED = _b("CRUSH_WALLBREAK_ENABLED", True)
WALL_DOMINANCE = _f("CRUSH_WALL_DOMINANCE", 2.5)   # wall OI >= 2.5x adjacent same-side OI
WALL_ARM_PTS = _f("CRUSH_WALL_ARM_PTS", 15.0)     # arm when spot within this of wall
WALL_DISARM_MULT = _f("CRUSH_WALL_DISARM_MULT", 2.0)  # disarm beyond ARM_PTS x this
WALLBREAK_OTM_MAX = _f("CRUSH_WALLBREAK_OTM_MAX", 30.0)  # pick strikes within this beyond wall
WALLBREAK_MAX_PER_WALL = _i("CRUSH_WALLBREAK_MAX_PER_WALL", 2)  # breaks per wall per day
# NOTE: wall-break arming AND entries are gated on the v2 entry windows
# (config.active_window()): A 11:00-12:30, B 15:30-15:58, cutoff 15:50.

# ---------------- v3 Module C: regime / day filter ----------------
# UNCALIBRATED scaffolding: "observe" logs the score only; "live" enforces it.
# Needs 20+ journaled live days before "live" is trustworthy.
REGIME_MODE = _s("CRUSH_REGIME_MODE", "observe")   # observe | live
REGIME_MIN_SCORE = _f("CRUSH_REGIME_MIN_SCORE", 0.35)  # live: below -> no new entries
REGIME_HIGH_SCORE = _f("CRUSH_REGIME_HIGH_SCORE", 0.70)  # live: at/above -> size up
REGIME_SIZE_MULT = _f("CRUSH_REGIME_SIZE_MULT", 1.5)
REGIME_TRADE_CAP = _f("CRUSH_REGIME_TRADE_CAP", 300.0)  # hard $ cap per trade, live mode

# ---------------- risk ----------------
RISK_PER_TRADE = _f("CRUSH_RISK_PER_TRADE", 200.0)  # $200 -> 10x = $2,000 target
MAX_TRADES_PER_DAY = _i("CRUSH_MAX_TRADES_PER_DAY", 2)
DAILY_STOP_PCT = _f("CRUSH_DAILY_STOP_PCT", 0.02)   # -2% of NetLiq -> kill switch, flatten
ACCOUNT_FALLBACK = _f("CRUSH_ACCOUNT_FALLBACK", 10000.0)

# ---------------- order handling ----------------
CHASE_ALLOWANCE = _f("CRUSH_CHASE_ALLOWANCE", 0.05)  # one $0.05 chase on unfilled entry
ENTRY_FILL_TIMEOUT = _f("CRUSH_ENTRY_FILL_TIMEOUT", 30)   # seconds before chase
ENTRY_GIVEUP_TIMEOUT = _f("CRUSH_ENTRY_GIVEUP_TIMEOUT", 60)  # seconds before cancel+stand down

# ---------------- line budget (hard cap 100, target <=75 sustained) ----------------
LINE_HARD_CAP = 100
LINE_TARGET = 75
SPX_LINES = 1
ACTIVE_WINDOW_PTS = _i("CRUSH_ACTIVE_WINDOW_PTS", 50)   # spot +/- 50, 5-pt steps, both rights
STRIKE_STEP = 5
CRUSH_BAND_MIN_OTM = _i("CRUSH_CRUSH_BAND_MIN_OTM", 55)  # far-OTM lottery zone
CRUSH_BAND_MAX_OTM = _i("CRUSH_CRUSH_BAND_MAX_OTM", 95)
CRUSH_BAND_STEP = _i("CRUSH_CRUSH_BAND_STEP", 10)
ALLLAST_MAX_LINES = _i("CRUSH_ALLLAST_MAX_LINES", 10)    # held position + top candidates
WING_SWEEP_RANGE = _i("CRUSH_WING_SWEEP_RANGE", 200)     # +/- pts for GEX completeness sweeps
WING_SWEEP_MIN = _i("CRUSH_WING_SWEEP_MIN", 15)
RECENTER_MIN = _i("CRUSH_RECENTER_MIN", 15)
GENERIC_TICKS = "100,101,106,107"  # verified set: volume, OI, bid/ask model Greeks

# ---------------- API pacer ----------------
PACER_MKT_PER_SEC = _f("CRUSH_PACER_MKT_PER_SEC", 3.0)  # snapshot/subscribe pacing
PACER_MAX_CONCURRENT = _i("CRUSH_PACER_MAX_CONCURRENT", 8)
SNAPSHOT_DWELL = _f("CRUSH_SNAPSHOT_DWELL", 2.5)        # seconds to let a snapshot fill

# ---------------- misc ----------------
UNDERLYING = "SPX"
EXCHANGE = "CBOE"
TRADING_CLASS = "SPXW"
LOOP_CADENCE_SEC = _f("CRUSH_LOOP_CADENCE_SEC", 5)
HEARTBEAT_SEC = _i("CRUSH_HEARTBEAT_SEC", 60)
GEX_RECOMPUTE_SEC = _i("CRUSH_GEX_RECOMPUTE_SEC", 300)  # full GEX refresh cadence
GAMMA_CHANGE_PCT = _f("CRUSH_GAMMA_CHANGE_PCT", 0.05)   # incremental GEX trigger
LOG_DIR = _s("CRUSH_LOG_DIR", "logs")

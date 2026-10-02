"""gex_bridge config — the single IBKR streaming connection.

One process streams everything both algos need:
  1  SPX index (spot)
  1  ES front futures ContFuture (basis = ES - SPX)
  42 SPXW 0DTE active window: spot +/-50, 5-pt strikes, both rights
  20 SPXW 0DTE crush band: 55-95 pts OTM, 10-pt steps, both sides/rights
 --  64 sustained streaming lines (target <=75, hard cap 100)

OI is frozen intraday (verified): ONE morning snapshot via paced one-shot
snapshot requests, then unsubscribe. GEX = frozen OI x live gamma (StableWall
estimator: 5-min wall clock, TWAP15 gamma, strike smoothing, 1.25x/3-eval
hysteresis, zones, confidence).

Sessions (ET):
  NY 09:30-16:05  full streaming, fresh 5-min wall evals, publish every 5s
  OVERNIGHT       chain streaming cancelled (lines freed), walls frozen with
                  wall_ts/stale=true + confidence decay, ES futures kept,
                  publish every 15s
  WEEKEND         Fri 17:00 -> Sun 17:55: sleep (no Globex)

Publishes ~/workspace/shared/levels.json ATOMICALLY (tmp + os.replace).
Consumers (algo/, algo_es/) NEVER stream; they read the file.

All overrides via env vars prefixed BRIDGE_.
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


# ---------------- IBKR connection (DATA ONLY — never places orders) ----------------
IB_HOST = _s("BRIDGE_IB_HOST", "127.0.0.1")
IB_PORT = _i("BRIDGE_IB_PORT", 7497)   # TWS paper; Gateway paper=4002
IB_CLIENT_ID = _i("BRIDGE_IB_CLIENT_ID", 1)   # THE streaming connection
CONNECT_TIMEOUT = _f("BRIDGE_CONNECT_TIMEOUT", 20)
CONNECT_RETRIES = _i("BRIDGE_CONNECT_RETRIES", 10)

# ---------------- shared dir ----------------
# All three processes (gex_bridge, algo, algo_es) must be siblings, as in
# the zips. Override with BRIDGE_SHARED_DIR if laid out differently.
SHARED_DIR = _s("BRIDGE_SHARED_DIR", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))
LEVELS_PATH = os.path.join(SHARED_DIR, "levels.json")
CONTRACTS_PATH = os.path.join(SHARED_DIR, "contracts.json")

# ---------------- session clock (ET) ----------------
NY_START = (9, 30)
NY_END = (16, 5)          # chain streaming cancelled at/after this
WEEKEND_SLEEP_UNTIL = (6, 17, 55)  # Sunday 17:55 ET (dow, hh, mm)


def session(now_et) -> str:
    """'ny' | 'overnight' | 'weekend'."""
    wd = now_et.weekday()
    t = (now_et.hour, now_et.minute)
    if wd == 5:                       # Saturday: fully closed
        return "weekend"
    if wd == 4 and t >= (17, 0):      # Friday 17:00 -> weekend
        return "weekend"
    if wd == 6 and t < (17, 55):      # Sunday before 17:55 -> weekend
        return "weekend"
    if NY_START <= t < NY_END and wd < 5:
        return "ny"
    return "overnight"


def next_wake(now_et):
    """Next ET datetime the bridge should be awake (Sun 17:55 after weekend)."""
    from datetime import timedelta
    t = now_et.replace(second=0, microsecond=0) + timedelta(minutes=5)
    for _ in range(3 * 24 * 12):
        if session(t) != "weekend":
            return t
        t += timedelta(minutes=5)
    return t

# ---------------- instruments ----------------
UNDERLYING = "SPX"
EXCHANGE = "CBOE"
TRADING_CLASS = "SPXW"
ES_SYMBOL = _s("BRIDGE_ES_SYMBOL", "ES")
ES_EXCHANGE = _s("BRIDGE_ES_EXCHANGE", "CME")

# ---------------- line budget (hard cap 100, target <=75 sustained) ----------------
# 1 SPX + 42 active window + 20 crush band + 1 ES futures = 64 sustained.
# OI snapshot + wing sweeps are paced one-shot snapshot requests
# (subscribe -> dwell -> cancel), never held lines. AllLast flow was
# tiebreak-only in v3 and is NOT streamed by the bridge.
LINE_HARD_CAP = 100
LINE_TARGET = 75
SPX_LINES = 1
ACTIVE_WINDOW_PTS = _i("BRIDGE_ACTIVE_WINDOW_PTS", 50)
STRIKE_STEP = 5
CRUSH_BAND_MIN_OTM = _i("BRIDGE_CRUSH_BAND_MIN_OTM", 55)
CRUSH_BAND_MAX_OTM = _i("BRIDGE_CRUSH_BAND_MAX_OTM", 95)
CRUSH_BAND_STEP = _i("BRIDGE_CRUSH_BAND_STEP", 10)
ALLLAST_MAX_LINES = 0                 # not streamed by the bridge
WING_SWEEP_RANGE = _i("BRIDGE_WING_SWEEP_RANGE", 200)
WING_SWEEP_MIN = _i("BRIDGE_WING_SWEEP_MIN", 15)
RECENTER_MIN = _i("BRIDGE_RECENTER_MIN", 15)
GENERIC_TICKS = "100,101,106,107"     # volume, OI, bid/ask model Greeks

# ---------------- API pacer ----------------
PACER_MKT_PER_SEC = _f("BRIDGE_PACER_MKT_PER_SEC", 3.0)
PACER_MAX_CONCURRENT = _i("BRIDGE_PACER_MAX_CONCURRENT", 8)
SNAPSHOT_DWELL = _f("BRIDGE_SNAPSHOT_DWELL", 2.5)

# ---------------- GEX / StableWall ----------------
GEX_MULT = _f("BRIDGE_GEX_MULT", 100.0)      # SPXW: 1 contract = 100x
GEX_EVAL_SEC = _i("BRIDGE_GEX_EVAL_SEC", 300)  # 5-min wall clock
GAMMA_CHANGE_PCT = _f("BRIDGE_GAMMA_CHANGE_PCT", 0.05)
FADE_TOUCH_PTS = _f("BRIDGE_FADE_TOUCH_PTS", 3.0)
CONF_DECAY_MIN = _f("BRIDGE_CONF_DECAY_MIN", 240.0)  # overnight decay horizon
CONF_DECAY_FLOOR = _f("BRIDGE_CONF_DECAY_FLOOR", 0.30)

# ---------------- 0DTE candidate screen (v3 10X-OTM, measured) ----------------
ENTRY_WINDOWS = (
    ("A", (11, 0), (12, 30), _i("BRIDGE_WIN_A_OTM_MIN", 40), _i("BRIDGE_WIN_A_OTM_MAX", 55)),
    ("B", (15, 30), (15, 58), _i("BRIDGE_WIN_B_OTM_MIN", 8), _i("BRIDGE_WIN_B_OTM_MAX", 12)),
)
ENTRY_CUTOFF = (15, 50)


def active_window(now_et):
    t = (now_et.hour, now_et.minute)
    if t >= ENTRY_CUTOFF:
        return None
    for wid, s, e, omin, omax in ENTRY_WINDOWS:
        if s <= t < e:
            return wid, omin, omax
    return None


def in_entry_window(now_et) -> bool:
    return active_window(now_et) is not None


ASK_MIN = _f("BRIDGE_ASK_MIN", 0.20)
ASK_MAX = _f("BRIDGE_ASK_MAX", 0.50)
DELTA_MAX = _f("BRIDGE_DELTA_MAX", 0.15)
SPREAD_MAX = _f("BRIDGE_SPREAD_MAX", 0.25)
ASK_Z_MAX = _f("BRIDGE_ASK_Z_MAX", -1.0)
ASK_Z_MIN = _f("BRIDGE_ASK_Z_MIN", -2.0)
ASK_Z_LOOKBACK_MIN = _i("BRIDGE_ASK_Z_LOOKBACK_MIN", 30)
IV_SPIKE_RATIO = _f("BRIDGE_IV_SPIKE_RATIO", 1.3)
IV_LOOKBACK_MIN = _i("BRIDGE_IV_LOOKBACK_MIN", 30)
MIN_ASK_HISTORY = _i("BRIDGE_MIN_ASK_HISTORY", 10)
TOP_CANDIDATES = _i("BRIDGE_TOP_CANDIDATES", 3)

# ---------------- wall-break screen (zone-edge version) ----------------
WALLBREAK_ENABLED = _b("BRIDGE_WALLBREAK_ENABLED", True)
WALL_DOMINANCE = _f("BRIDGE_WALL_DOMINANCE", 2.5)
WALL_ARM_PTS = _f("BRIDGE_WALL_ARM_PTS", 15.0)
WALL_DISARM_MULT = _f("BRIDGE_WALL_DISARM_MULT", 2.0)
WALLBREAK_OTM_MAX = _f("BRIDGE_WALLBREAK_OTM_MAX", 30.0)
WALLBREAK_MAX_PER_WALL = _i("BRIDGE_WALLBREAK_MAX_PER_WALL", 2)

# ---------------- regime (Module C, observe-mode scaffolding) ----------------
REGIME_MODE = _s("BRIDGE_REGIME_MODE", "observe")

# ---------------- publish ----------------
PUBLISH_NY_SEC = _f("BRIDGE_PUBLISH_NY_SEC", 5)
PUBLISH_ON_SEC = _f("BRIDGE_PUBLISH_ON_SEC", 15)
LOOP_CADENCE_SEC = _f("BRIDGE_LOOP_CADENCE_SEC", 5)
HEARTBEAT_SEC = _i("BRIDGE_HEARTBEAT_SEC", 60)
LOG_DIR = _s("BRIDGE_LOG_DIR", "logs")

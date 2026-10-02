#!/usr/bin/env python3
"""FunTime regression harness.  Run:  python regression_funtime.py
Exit code 0 = all checks pass, 1 = at least one failure.

v1.00 2026-10-01  First build.  Checks:
  R1  no tracked file is base64-encoded (the repo was uploaded that way once
      and nothing could run)
  R2  every .py file compiles
  R3  .env is git-ignored and NOT tracked; .env.example-style secrets absent
  R4  no MT5 credential literals in tracked files (password / login values)
  R5  .env loader: each component's config.py reads <repo>/.env and
      <component>/.env, real env vars win, values never printed
  R6  every local `import x` / `from x import` in each component resolves to
      a file in that component (catches renamed / deleted modules)
  R7  run_all.py components point at files that exist
  R8  ported ES strategies (algo_es/strategies) construct and run 3,000
      synthetic M1 bars with full state without raising; any Signal fired
      has the stop and target on the correct sides of entry
  R9  FiveDMAStruct (5DMA + structure) runs on synthetic bars without raising

v1.01 2026-10-01  [GLOBEXWIRE] R10: end-to-end dry run of algo_es main.run_session
      against a fake MT5 terminal (algo_es/sim_globex_test.py): a strategy signal
      becomes GLOBEX_SIGNAL -> CANDIDATE -> SIM_ENTER -> CLOSED target-2; a signal
      with the stop on the wrong side is rejected; all 6 real strategies run in
      the loop without raising.  Verified to FAIL against the unwired v1.01 main.py.
v1.02 2026-10-01  [FIXEDQTY] R11: ES sizing is exactly ES_FIXED_QTY (1) for every
      stop distance incl. overnight halving; 0DTE sizing is exactly CRUSH_FIXED_QTY
      (10) for every premium incl. regime size-up.
v1.03 2026-10-01  [SPREAD10X] R12: 0DTE 10-pt debit spreads end-to-end in a dry run
      of algo main.run_session (algo/sim_spread_test.py, S1-S8): 10X lock + trail,
      cap fill, lock exits at >= 10X, >$1.00 debit refused, lowest debit chosen,
      naked mode intact, 10 contracts, no exit before 10X except the 15:55 flat.
v1.04 2026-10-01  [RESILIENT] R13: outages. 0DTE (algo/sim_outage_test.py O1-O3):
      IBKR drop mid-trade carries + resumes the position, stale bridge data
      freezes exits, IBKR down at start retries every 60s. ES (sim_globex_test D):
      MT5 quotes vanish -> reconnect, loop survives. Supervisor restarts a
      component forever (was: gave up after 5). Bridge rebuilds subscriptions
      after an IBKR reconnect. O1/O2 and D verified to FAIL on the pre-v1.05 code.
v1.05 2026-10-02  [HALF10X] R12 adds S9: half the spreads sold at exactly 10X by
      the resting order, the runner exits separately; nothing sold before 10X.
"""
import ast, base64, datetime as dt, math, os, random, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
COMPONENTS = ("algo", "algo_es", "gex_bridge")
fails, passes = [], []


def check(name, ok, detail=""):
    (passes if ok else fails).append(name)
    print(("PASS " if ok else "FAIL ") + name + (("  -- " + detail) if detail else ""))


def tracked():
    out = subprocess.run(["git", "ls-files"], cwd=HERE, capture_output=True, text=True)
    return [f for f in out.stdout.split() if os.path.isfile(os.path.join(HERE, f))]


FILES = tracked()

# R1 base64 ----------------------------------------------------------------
b64 = []
for f in FILES:
    raw = open(os.path.join(HERE, f), "rb").read().strip()
    if len(raw) > 40 and re.fullmatch(rb"[A-Za-z0-9+/=\s]+", raw):
        try:
            base64.b64decode(raw).decode("utf-8")
            b64.append(f)
        except Exception:
            pass
check("R1 no base64-encoded files", not b64, ", ".join(b64[:5]))

# R2 compile ---------------------------------------------------------------
bad = []
for root, _, names in os.walk(HERE):
    if any(p in root for p in (".git", "venv", "__pycache__", "_to_delete")):
        continue
    for n in names:
        if n.endswith(".py"):
            p = os.path.join(root, n)
            try:
                compile(open(p, encoding="utf-8").read(), p, "exec")
            except SyntaxError as e:
                bad.append("%s:%s" % (os.path.relpath(p, HERE), e.lineno))
check("R2 all .py compile", not bad, ", ".join(bad))

# R3 .env ignored and untracked ---------------------------------------------
ign = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=HERE).returncode == 0
check("R3 .env git-ignored and untracked", ign and ".env" not in FILES)

# R4 no credential literals -------------------------------------------------
leaks = []
pat = re.compile(r"MT5_(PASSWORD|LOGIN)\s*[=:]\s*['\"]?[A-Za-z0-9@!#$%^&*]{4,}")
for f in FILES:
    if f.endswith((".py", ".bat", ".md", ".txt", ".json", ".cmd")):
        for i, line in enumerate(open(os.path.join(HERE, f), encoding="utf-8", errors="ignore"), 1):
            m = pat.search(line)
            if m and not re.search(r"your|\*|12345678|here|<", line, re.I):
                leaks.append("%s:%d" % (f, i))
check("R4 no MT5 credential literals in tracked files", not leaks, ", ".join(leaks))

# R5 .env loader ------------------------------------------------------------
r5 = []
for comp in COMPONENTS:
    src = os.path.join(HERE, comp, "config.py")
    if "_load_dotenv" not in open(src, encoding="utf-8").read():
        r5.append(comp + ": no loader")
        continue
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, comp))
        shutil.copy(src, os.path.join(tmp, comp, "config.py"))
        with open(os.path.join(tmp, ".env"), "w") as fh:
            fh.write("# c\nRT_A=from_repo\nRT_B=repo\nRT_WIN=file\n")
        with open(os.path.join(tmp, comp, ".env"), "w") as fh:
            fh.write("RT_B=component\n")
        code = ("import os,config;print(os.environ.get('RT_A'),os.environ.get('RT_B'),"
                "os.environ.get('RT_WIN'))")
        env = {k: v for k, v in os.environ.items() if not k.startswith("RT_")}
        env["RT_WIN"] = "real"
        out = subprocess.run([sys.executable, "-c", code], cwd=os.path.join(tmp, comp),
                             env=env, capture_output=True, text=True)
        got = out.stdout.strip()
        if got != "from_repo component real":
            r5.append("%s: got %r %s" % (comp, got, out.stderr.strip()[-200:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
check("R5 .env loader (repo + component, real env wins)", not r5, "; ".join(r5))

# R6 local imports resolve --------------------------------------------------
r6 = []
for comp in COMPONENTS:
    cdir = os.path.join(HERE, comp)
    local = {n[:-3] for n in os.listdir(cdir) if n.endswith(".py")} | \
            {n for n in os.listdir(cdir) if os.path.isdir(os.path.join(cdir, n))}
    known_local_like = local | {"config"}
    for n in os.listdir(cdir):
        if not n.endswith(".py"):
            continue
        if "sys.path.insert" in open(os.path.join(cdir, n), encoding="utf-8").read():
            continue   # cross-component smoke scripts add sibling paths on purpose
        tree = ast.parse(open(os.path.join(cdir, n), encoding="utf-8").read())
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            for m in mods:
                # flag only names that look like sibling modules of ANY component
                sib = any(os.path.exists(os.path.join(HERE, c, m + ".py")) for c in COMPONENTS)
                if sib and m not in known_local_like:
                    r6.append("%s/%s imports %s (not in %s)" % (comp, n, m, comp))
check("R6 local imports resolve inside each component", not r6, "; ".join(r6[:5]))

# R7 run_all components -----------------------------------------------------
ra = open(os.path.join(HERE, "run_all.py"), encoding="utf-8").read()
paths = re.findall(r'"([\w/]+\.py)"', ra)
missing = [p for p in paths if not os.path.exists(os.path.join(HERE, p))]
check("R7 run_all.py component scripts exist", paths and not missing, ", ".join(missing))


# synthetic bars ------------------------------------------------------------
def synth(n=3000, seed=7):
    random.seed(seed)
    t0 = dt.datetime(2026, 9, 28, 13, 30, tzinfo=dt.timezone.utc)
    px, out = 7700.0, []
    for i in range(n):
        o = px
        px += random.gauss(0, 2.5) + 4 * math.sin(i / 45.0)
        h = max(o, px) + abs(random.gauss(0, 1.2))
        lo = min(o, px) - abs(random.gauss(0, 1.2))
        out.append({"time": t0 + dt.timedelta(minutes=i), "open": o, "high": h,
                    "low": lo, "close": px, "volume": 100 + random.randint(0, 400)})
    return out


def agg(bars, k):
    res = []
    for i in range(0, len(bars) - k + 1, k):
        g = bars[i:i + k]
        res.append({"time": g[0]["time"], "open": g[0]["open"], "high": max(b["high"] for b in g),
                    "low": min(b["low"] for b in g), "close": g[-1]["close"],
                    "volume": sum(b["volume"] for b in g)})
    return res


# R8 ported strategies ------------------------------------------------------
sys.path.insert(0, os.path.join(HERE, "algo_es"))
r8, fired = [], {}
try:
    from strategies import STRATEGIES
    bars = synth()
    for name, S in STRATEGIES.items():
        s = S(None)
        fired[name] = 0
        for i in range(60, len(bars)):
            b = bars[i]
            hist = bars[:i + 1]
            state = {"bars": hist[-400:], "bars_m1": hist[-400:], "bars_h1": agg(hist[-1200:], 60),
                     "last_price": b["close"], "now": b["time"],
                     "daily_closes": [7650, 7680, 7700, 7720, 7690, 7710],
                     "gex": {"call_wall": round(b["close"] / 25) * 25 + 25,
                             "put_wall": round(b["close"] / 25) * 25 - 25,
                             "flip": b["close"] - 10, "regime": "negative"}}
            try:
                sig = s.on_bar(b, state)
            except Exception as e:
                r8.append("%s raised %s: %s" % (name, type(e).__name__, str(e)[:120]))
                break
            if sig:
                fired[name] += 1
                long_ = sig.side.lower() in ("buy", "long")
                if sig.stop_px and sig.target_px and (
                        (long_ and not (sig.stop_px < sig.entry_px < sig.target_px)) or
                        (not long_ and not (sig.target_px < sig.entry_px < sig.stop_px))):
                    r8.append("%s bad levels %s e=%.2f sl=%.2f tp=%.2f" % (
                        name, sig.side, sig.entry_px, sig.stop_px, sig.target_px))
except Exception as e:
    r8.append("import: %s" % e)
check("R8 ported strategies run clean on 3,000 bars", not r8,
      "; ".join(r8[:4]) or "signals " + str(fired))

# R9 FiveDMAStruct ----------------------------------------------------------
try:
    from strategies.fivedma_struct import FiveDMAStruct, Bar
    f, n = FiveDMAStruct(), 0
    for b in agg(synth(), 30):
        n += bool(f.on_bar(Bar(str(b["time"]), b["open"], b["high"], b["low"], b["close"])))
    check("R9 FiveDMAStruct runs", True, "signals %d" % n)
except Exception as e:
    check("R9 FiveDMAStruct runs", False, "%s: %s" % (type(e).__name__, e))

# R10 end-to-end Globex sleeve wiring (fake MT5, dry run) ------------------
try:
    import numpy  # noqa: F401
    out = subprocess.run([sys.executable, "sim_globex_test.py"], cwd=os.path.join(HERE, "algo_es"),
                         capture_output=True, text=True, timeout=300)
    tail = [l for l in out.stdout.splitlines() if l.startswith(("FAIL", "SIM"))]
    check("R10 Globex sleeve wired end-to-end (fake MT5 dry run)", out.returncode == 0,
          "; ".join(tail[-4:]) or out.stderr.strip()[-300:])
except ImportError:
    check("R10 Globex sleeve wired end-to-end (fake MT5 dry run)", False, "numpy not installed")

# R11 fixed contract sizes ---------------------------------------------------
r11 = []
code_es = ("import config,strategy;"
           "print(sorted({strategy.size_contracts(x) for x in (0,0.5,1,2,4,10,40)}),"
           "max(1, strategy.size_contracts(1)//2))")
o = subprocess.run([sys.executable, "-c", code_es], cwd=os.path.join(HERE, "algo_es"),
                   capture_output=True, text=True)
if o.stdout.strip() != "[1] 1":
    r11.append("ES sizes %r %s" % (o.stdout.strip(), o.stderr.strip()[-150:]))
code_od = ("import sys,types;sys.modules.setdefault('ib_insync',types.ModuleType('ib_insync'));"
           "import config,risk;r=risk.RiskManager.__new__(risk.RiskManager);r.size_mult=1.0;"
           "a={r.size_qty(x) for x in (0.05,0.2,0.35,0.5,2.0)};r.size_mult=1.5;"
           "a|={r.size_qty(x) for x in (0.05,0.2,0.5)};print(sorted(a))")
o = subprocess.run([sys.executable, "-c", code_od], cwd=os.path.join(HERE, "algo"),
                   capture_output=True, text=True)
if o.stdout.strip() != "[10]":
    r11.append("0DTE sizes %r %s" % (o.stdout.strip(), o.stderr.strip()[-150:]))
check("R11 fixed sizes: ES 1 contract, 0DTE 10 contracts", not r11, "; ".join(r11))

# R12 0DTE spread vehicle ---------------------------------------------------
try:
    import ib_insync  # noqa: F401
    out = subprocess.run([sys.executable, "sim_spread_test.py"], cwd=os.path.join(HERE, "algo"),
                         capture_output=True, text=True, timeout=300)
    tail = [l for l in out.stdout.splitlines() if l.startswith(("FAIL", "SIM"))]
    check("R12 0DTE 10-pt spreads with 10X lock (dry run S1-S8)", out.returncode == 0,
          "; ".join(tail[-5:]) or out.stderr.strip()[-300:])
except ImportError:
    check("R12 0DTE 10-pt spreads with 10X lock (dry run S1-S8)", False,
          "ib_insync not installed (pip install -r requirements.txt)")

# R13 outage behaviour --------------------------------------------------------
r13 = []
out = subprocess.run([sys.executable, "sim_outage_test.py"], cwd=os.path.join(HERE, "algo"),
                     capture_output=True, text=True, timeout=300)
if out.returncode != 0:
    r13.append("0DTE: " + "; ".join(l for l in out.stdout.splitlines() if l.startswith("FAIL"))[:300]
               or out.stderr.strip()[-200:])
code_sup = r"""
import sys, types, threading, time
sys.argv = ['run_all.py']
import run_all
starts = []
class P:
    def __init__(self, *a, **k):
        starts.append(1)
        if len(starts) > 8: raise SystemExit
        self.stdout = []; self.returncode = 1
    def wait(self): return 1
run_all.subprocess.Popen = P
run_all.time.sleep = lambda s: None
try:
    run_all.run_component('x', 'gex_bridge/main.py', [])
except SystemExit:
    pass
print(len(starts))
"""
o = subprocess.run([sys.executable, "-c", code_sup], cwd=HERE, capture_output=True, text=True, timeout=60)
last = (o.stdout.strip().splitlines() or [""])[-1]
if last != "9":
    r13.append("supervisor restarts %r (want unlimited) %s" % (last, o.stderr.strip()[-150:]))
br = open(os.path.join(HERE, "gex_bridge", "main.py"), encoding="utf-8").read()
if "was_up = ib.isConnected()" not in br or "state.clear()" not in br:
    r13.append("bridge does not rebuild subscriptions after an IBKR reconnect")
check("R13 outages: IBKR/MT5 down -> retry, carry, freeze; supervisor never gives up",
      not r13, "; ".join(r13))

# R14 launcher arguments accepted by every component ------------------------
r14 = []
for live in (False, True):
    code = ("import sys,ast;sys.argv=['run_all.py']+(['--live'] if %r else ['--dry-run']);"
            "import run_all;print(repr(run_all.COMPONENTS))") % live
    o = subprocess.run([sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True)
    try:
        comps = ast.literal_eval(o.stdout.strip().splitlines()[-1])
    except Exception:
        r14.append("cannot read COMPONENTS: %s" % o.stderr.strip()[-150:]); continue
    for name, (script, args) in comps:
        src = open(os.path.join(HERE, script), encoding="utf-8").read()
        flags = set(re.findall(r'add_argument\(\s*"(--[\w-]+)"', src))
        bad = [a for a in args if a.startswith("--") and a not in flags]
        if bad:
            r14.append("%s %s: %s not accepted" % ("live" if live else "dry", name, bad))
check("R14 run_all.py arguments accepted by every component (dry + live)", not r14, "; ".join(r14))

# R15 DMA-520 sees a sweep late in the forming candle ----------------------
code15 = r"""
import sys, datetime as dt
sys.path.insert(0, '.')
import strategies.dma520 as m
clock = {'t': 0.0}
m.time = type('T', (), {'time': staticmethod(lambda: clock['t'])})
s = m.Strategy({'globex_only': False, 'tfs': ['1h']})
ET = dt.timezone(dt.timedelta(hours=-4))
t0 = dt.datetime(2026, 9, 1, 10, 0, tzinfo=ET)
dc = [7500.0] * 15 + [7520.0] * 5           # 5DMA 7520, 20DMA 7505
hist = [{'time': t0 - dt.timedelta(hours=k), 'open': 7510, 'high': 7540 + (k % 3),
         'low': 7480 - (k % 4), 'close': 7510 + (k % 5) - 2} for k in range(30, 0, -1)]
fired = None
for i in range(60):                          # minutes of the forming 10:00 candle
    hi = 7512 + (20 if i >= 40 else 0)       # sweeps ABOVE both lines at minute 40
    lo = 7508 - (25 if i >= 40 else 0)       # ... and below both (bigger under)
    form = {'time': t0, 'open': 7510, 'high': hi, 'low': lo, 'close': 7512}
    m1t = t0 + dt.timedelta(minutes=i)
    clock['t'] = (m1t + dt.timedelta(minutes=1)).timestamp()
    st = {'tf_bars': {'1h': hist + [form]}, 'daily_closes': dc,
          'bars_m1': [{'time': m1t - dt.timedelta(minutes=1), 'close': 7511},
                      {'time': m1t, 'close': 7512}]}
    sig = s.on_bar(st['bars_m1'][-1], st)
    if sig:
        fired = i; break
print(fired)
"""
o = subprocess.run([sys.executable, "-c", code15], cwd=os.path.join(HERE, "algo_es"),
                   capture_output=True, text=True)
got = (o.stdout.strip().splitlines() or ["?"])[-1]
check("R15 DMA-520 fires on a sweep late in the forming candle", got == "40",
      "fired at minute %s (want 40) %s" % (got, o.stderr.strip()[-150:]))

# R16 5DMA intraday: M1-close entry, every touch, NY+GEX block -------------
code16 = r"""
import sys, datetime as dt
sys.path.insert(0, '.')
import globex
from zoneinfo import ZoneInfo
ET = ZoneInfo('America/New_York')
sl = globex.GlobexSleeve(lambda *a, **k: None, enabled='fivedma', all_hours=True)
# 25 completed days trending up to 7600; yesterday closes 7700 > 5DMA (uptrend)
d1 = []
for k in range(25):
    c = 7400 + 12 * k
    d1.append({'time': dt.datetime(2026, 8, 1, tzinfo=ET) + dt.timedelta(days=k),
               'open': c - 5, 'high': c + 30, 'low': c - 30, 'close': c})
d1[-1]['close'] = 7700.0
f5 = sl.strats['fivedma']
f5.load_days([globex._fbar(b) for b in d1])
st = f5.setup()
if not st:
    # make confluence true: put a round level on the 5DMA
    pass
side, dma5, atr = f5.setup() or (None, None, None)
today = {'time': dt.datetime(2026, 8, 26, tzinfo=ET), 'open': 7700, 'high': 7720, 'low': 7600, 'close': 7700}
class G:  # GEX active
    call_wall = put_wall = flip = None; stale = False
    def gamma_regime(self): return '+'
def run(times_prices, gex):
    hits = []
    for t, (hi, lo, cl) in times_prices:
        m1 = [{'time': t - dt.timedelta(minutes=1), 'open': cl, 'high': hi, 'low': lo, 'close': cl, 'volume': 1},
              {'time': t, 'open': cl, 'high': cl, 'low': cl, 'close': cl, 'volume': 0}]
        sl.load({'m1': m1, 'd1': d1 + [today]})
        for g in sl.step(t, cl, gex):
            hits.append((t.strftime('%H:%M'), g['name']))
            sl.on_position_closed('globex_fivedma')
    return hits
if side != 'long':
    print('setup', side, dma5); raise SystemExit
D = dma5
base = dt.datetime(2026, 8, 26, 2, 0, tzinfo=ET)          # Globex hours
seq = [(D + 20, D + 10, D + 15), (D + 5, D - 1, D + 3),   # touch + close back -> entry
       (D + 4, D - 2, D + 2),                              # still on the line: no re-entry
       (D + 25, D + 12, D + 20),                           # traded away -> re-armed
       (D + 8, D - 1, D + 4)]                              # second touch -> 2nd entry
g = run([(base + dt.timedelta(minutes=i), p) for i, p in enumerate(seq)], None)
ny = dt.datetime(2026, 8, 26, 10, 0, tzinfo=ET)            # NY session + GEX
f5._need_away = False
b = run([(ny + dt.timedelta(minutes=i), p) for i, p in enumerate(seq)], G())
print(len(g), len(b))
"""
o = subprocess.run([sys.executable, "-c", code16], cwd=os.path.join(HERE, "algo_es"),
                   capture_output=True, text=True)
got = (o.stdout.strip().splitlines() or ["?"])[-1]
check("R16 5DMA M1-close entry, every touch counts, blocked in NY while GEX active",
      got == "2 0", "globex/ny entries %r (want '2 0') %s" % (got, o.stderr.strip()[-200:]))

# R17 overnight fade is restart-safe -----------------------------------------
code17 = r"""
import sys, os, tempfile, datetime as dt
sys.path.insert(0, '.')
import config
config.LOG_DIR = tempfile.mkdtemp()
from strategy import OvernightPA
from zoneinfo import ZoneInfo
ET = ZoneInfo('America/New_York')
t0 = dt.datetime(2026, 10, 1, 18, 0, tzinfo=ET)
# ON range built in < 2-pt steps (a 2-pt jump would be a sweep and block C)
hi = [7742, 7743.5, 7745, 7746.5] + [7746.5] * 56
lo = [7735, 7733.5, 7732, 7730.5, 7730] + [7730] * 55
bars = [(t0 + dt.timedelta(minutes=i), 7740, min(hi[i], 7746.5), max(lo[i], 7730), 7740)
        for i in range(60)]                       # ON range 7730-7746.5 (16.5 pts)
now = dt.datetime(2026, 10, 2, 0, 51, tzinfo=ET)
out = []
def start():
    pa = OvernightPA(); pa.note_bars(bars); return pa
pa = start()
out.append(bool(pa.evaluate_c(now, 7745.88)))     # at the high on startup -> no
pa = start()
out.append(bool(pa.evaluate_c(now, 7745.88)))     # restart, still there -> no
out.append(bool(pa.evaluate_c(now, 7740.0)))      # away
out.append(bool(pa.evaluate_c(now, 7746.0)))      # back -> fade #1
pa = start()                                       # restart at the high
out.append(bool(pa.evaluate_c(now, 7746.0)))      # -> no
pa.evaluate_c(now, 7740.0)
out.append(bool(pa.evaluate_c(now, 7746.2)))      # fresh touch -> fade #2
pa = start(); pa.evaluate_c(now, 7740.0)
out.append(bool(pa.evaluate_c(now, 7746.0)))      # cap 2 survived restart -> no
print(''.join('1' if x else '0' for x in out))
"""
o = subprocess.run([sys.executable, "-c", code17], cwd=os.path.join(HERE, "algo_es"),
                   capture_output=True, text=True)
got = (o.stdout.strip().splitlines() or ["?"])[-1]
check("R17 overnight fade restart-safe (no re-sell on restart, cap survives)",
      got == "0001010", "got %r want '0001010' %s" % (got, o.stderr.strip()[-200:]))

# R18 MT5 bars: closed only, final values -----------------------------------
code18 = r"""
import sys, asyncio, types
sys.path.insert(0, '.')
import numpy as np
import mt5_data
dt = np.dtype([('time','i8'),('open','f8'),('high','f8'),('low','f8'),('close','f8'),('tick_volume','i8')])
seq = [np.array([(60, 1, 2, 0, 1, 1), (120, 1, 1.5, 0.5, 1, 1)], dtype=dt),      # 120 forming, high 1.5
       np.array([(60, 1, 2, 0, 1, 1), (120, 1, 9.0, -5, 1, 1), (180, 1, 1, 1, 1, 1)], dtype=dt)]
class M:
    TIMEFRAME_M1 = 1
    def __init__(self): self.k = 0
    def copy_rates_from_pos(self, *a):
        r = seq[min(self.k, 1)]; self.k += 1; return r
f = mt5_data.MT5DataFeed(lambda *a, **k: None)
f.mt5 = M(); f.symbol = 'X'; f.ok = True
asyncio.run(f.sync()); asyncio.run(f.sync())
b = {int(x[0].timestamp()): (x[2], x[3]) for x in f.bars}
print(sorted(b), b.get(120))
"""
o = subprocess.run([sys.executable, "-c", code18], cwd=os.path.join(HERE, "algo_es"),
                   capture_output=True, text=True)
got = (o.stdout.strip().splitlines() or ["?"])[-1]
check("R18 MT5 M1 bars stored closed and final (no frozen partial bar)",
      got == "[60, 120] (9.0, -5.0)", "got %r %s" % (got, o.stderr.strip()[-200:]))

# R19 dashboard: read-only, serves state from files, no broker imports ------
r19 = []
src19 = open(os.path.join(HERE, "dashboard", "server.py"), encoding="utf-8").read()
for bad in ("ib_insync", "MetaTrader5", "placeOrder", "order_send"):
    if bad in src19:
        r19.append("server.py references %s" % bad)
code19 = r"""
import os, sys, json, tempfile, threading, urllib.request, urllib.error
sh, lg = tempfile.mkdtemp(), tempfile.mkdtemp()
os.environ.update(SHARED_DIR=sh, DASH_LOG_DIR=lg, DASH_HOST='127.0.0.1', DASH_PORT='0')
json.dump({'session': 'ny', 'spx': 6700.0, 'chain_frame': {'x': 1}}, open(os.path.join(sh, 'levels.json'), 'w'))
json.dump({'spot': 6700, 'strikes': [6690, 6700], 'call': [1, 2], 'put': [1, 1], 'net': [0, 1]},
          open(os.path.join(sh, 'gex_profile.json'), 'w'))
json.dump({'es': 6750, 'position': {'side': 'long'}}, open(os.path.join(lg, 'status_es.json'), 'w'))
sys.path.insert(0, 'dashboard')
import server
srv = server.ThreadingHTTPServer(('127.0.0.1', 0), server.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = 'http://127.0.0.1:%d' % srv.server_address[1]
res = []
res.append(urllib.request.urlopen(base + '/').status)
js = urllib.request.urlopen(base + '/plotly.min.js')
res.append(int(js.status == 200 and b'plotly' in js.read(400)))
st = json.load(urllib.request.urlopen(base + '/api/state'))
res.append(int(st['levels']['spx'] == 6700.0 and 'chain_frame' not in st['levels']
               and st['profile']['strikes'] == [6690, 6700] and st['es']['position']['side'] == 'long'))
for m in ('POST', 'PUT', 'DELETE'):
    try:
        urllib.request.urlopen(urllib.request.Request(base + '/api/state', data=b'x', method=m))
        res.append('open')
    except urllib.error.HTTPError as e:
        res.append(e.code)
try:
    urllib.request.urlopen(base + '/../.env'); res.append('open')
except urllib.error.HTTPError as e:
    res.append(e.code)
srv.shutdown()
print(res)
"""
o = subprocess.run([sys.executable, "-c", code19], cwd=HERE, capture_output=True, text=True, timeout=60)
got = (o.stdout.strip().splitlines() or ["?"])[-1]
if got != "[200, 1, 1, 405, 405, 405, 404]":
    r19.append("server behaviour %r %s" % (got, o.stderr.strip()[-200:]))
check("R19 dashboard read-only: GET works from files, writes 405, no file paths, no broker code",
      not r19, "; ".join(r19))

# R20 TWS + Gateway: falls back 7497 -> 4002, remembers the good port ------
r20 = []
code20 = r"""
import sys, types, asyncio
fake = types.ModuleType('ib_insync')
class IB:
    up = {4002}
    def __init__(s): s.c = False; s.tried = []
    async def connectAsync(s, h, p, clientId=0, timeout=0):
        s.tried.append(p)
        if p not in IB.up: raise ConnectionRefusedError('refused')
        s.c = True
    def isConnected(s): return s.c
    def disconnect(s): s.c = False
fake.IB = IB
sys.modules['ib_insync'] = fake
out = []
for comp in ('gex_bridge', 'algo'):
    for m in ('config', 'ibkr_conn'): sys.modules.pop(m, None)
    sys.path.insert(0, comp)
    import config, ibkr_conn
    sys.path.pop(0)
    ib = IB()
    p1 = asyncio.run(ibkr_conn._try_ports(ib, 't'))
    ib2 = IB(); p2 = asyncio.run(ibkr_conn._try_ports(ib2, 't'))
    IB.up = set()
    try: asyncio.run(ibkr_conn._try_ports(IB(), 't')); bad = 'no-raise'
    except ConnectionError: bad = 'raise'
    IB.up = {4002}
    out.append((config.IB_PORTS[:2], p1, ib.tried, ib2.tried, bad))
print(out)
"""
o = subprocess.run([sys.executable, "-c", code20], cwd=HERE, capture_output=True, text=True, timeout=60,
                   env=dict(os.environ, BRIDGE_IB_PORTS="", CRUSH_IB_PORTS=""))
got = (o.stdout.strip().splitlines() or ["?"])[-1]
want = str([([7497, 4002], 4002, [7497, 4002], [4002], 'raise')] * 2)
if got != want:
    r20.append("port fallback %r %s" % (got, o.stderr.strip()[-300:]))
check("R20 IBKR TWS(7497) + Gateway(4002) fallback, last good port first, both components",
      not r20, "; ".join(r20))

# R21 BB-2C dual TF + M1-close entry during the 2nd candle ----------------
r21 = []
code21 = r"""
import sys, os, datetime as dt, time as _t
sys.path.insert(0, 'algo_es')
import globex
out = []
sl = globex.GlobexSleeve(lambda *a, **k: None, enabled='bb2c')
out.append(sorted(sl.strats))
out.append((sl.strats['bb2c'].struct_tf, sl.strats['bb2c_h1'].struct_tf,
            sl.strats['bb2c_h1'].entry_trigger))
base = 1_700_000_000 - (1_700_000_000 % 3600)
h1 = [{'time': base + i*3600, 'open': 6700, 'high': 6702, 'low': 6698,
       'close': 6700 + (0.5 if i % 2 else -0.5)} for i in range(30)]
h1.append({'time': base + 30*3600, 'open': 6700, 'high': 6700, 'low': 6660, 'close': 6662})  # A
B = base + 31*3600
h1.append({'time': B, 'open': 6662, 'high': 6692, 'low': 6660, 'close': 6690})               # forming B
sl.bars.update(h1=h1, m15=[], h4=[], d1=[{'time': base - k*86400, 'open': 6700, 'high': 6700,
               'low': 6700, 'close': 6700} for k in range(6, 0, -1)])
now = dt.datetime(2026, 10, 2, 3, 0)
seq = []
for k, c in enumerate((6663, 6690, 6691)):      # M1 closes during B
    m = {'time': B + k*60, 'open': c, 'high': c, 'low': c, 'close': c}
    sl.bars['m1'] = [m, dict(m, time=m['time'] + 60)]           # + forming M1
    _t.time = lambda t=m['time']: t + 70
    got = []
    for name, s in sl.strats.items():
        sig = s.on_bar(m, sl._state(name, now, c, None))
        if sig: got.append((name, sig.side, sig.entry_px, sig.target_px, sig.stop_px))
    seq.append(got)
out.append(seq)
print(out)
"""
o = subprocess.run([sys.executable, "-c", code21], cwd=HERE, capture_output=True, text=True, timeout=60,
                   env=dict(os.environ, ES_BB2C_TFS="4h,1h", ES_BB2C_ENTRY="m1_b"))
got = (o.stdout.strip().splitlines() or ["?"])[-1]
want = ("[['bb2c', 'bb2c_h1'], ('4h', '1h', 'm1_b'), "
        "[[], [('bb2c_h1', 'buy', 6690.0, 6700.0, 6670.0)], []]]")
if got != want:
    r21.append("dual/m1_b %r %s" % (got, o.stderr.strip()[-300:]))
o = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0,'algo_es'); import globex; "
                    "g=globex.GlobexSleeve(lambda *a, **k: None, enabled='bb2c'); "
                    "print(sorted(g.strats), g.strats['bb2c'].entry_trigger)"],
                   cwd=HERE, capture_output=True, text=True, timeout=60,
                   env=dict(os.environ, ES_BB2C_TFS="4h", ES_BB2C_ENTRY="first_m1"))
if o.stdout.strip() != "['bb2c'] first_m1":
    r21.append("ES_BB2C_TFS=4h/first_m1 gave %r %s" % (o.stdout.strip(), o.stderr.strip()[-200:]))
check("R21 BB-2C on H4+H1; entry = first M1 close back inside the band during the 2nd candle; target 5DMA",
      not r21, "; ".join(r21))

# R22 bridge OI/wing scans never use snapshot=True with generic ticks ------
r22 = []
code22 = r"""
import sys, types, asyncio
fake = types.ModuleType('ib_insync')
class T:
    def __init__(s): s.callOpenInterest = s.putOpenInterest = None; s.bid = s.ask = None
    askGreeks = bidGreeks = lastGreeks = modelGreeks = None
class IB:
    def __init__(s): s.calls = []; s.open = 0; s.peak = 0
    def reqMktData(s, c, genericTickList='', snapshot=False, regulatorySnapshot=False):
        if snapshot and genericTickList: raise RuntimeError('321 snapshot+generic')
        s.calls.append(snapshot); s.open += 1; s.peak = max(s.peak, s.open)
        t = T()
        async def fill():
            await asyncio.sleep(0.3)
            if c.right == 'C': t.callOpenInterest = 100.0
            else: t.putOpenInterest = 50.0
        asyncio.get_event_loop().create_task(fill())
        return t
    def cancelMktData(s, c): s.open -= 1
class C:
    def __init__(s, k, r): s.strike = k; s.right = r
for n in ('IB', 'Contract', 'Index', 'Ticker'): setattr(fake, n, IB if n == 'IB' else object)
sys.modules['ib_insync'] = fake
sys.path.insert(0, 'gex_bridge')
import config; config.BRIEF_MAX_LINES = 5; config.PACER_MKT_PER_SEC = 1000
import chain
from pacer import Pacer
async def go():
    ib = IB(); cs = chain.ChainStream(ib, Pacer(1000, 50))
    cs.contracts = {(6700.0 + 5*i, r): C(6700.0 + 5*i, r) for i in range(12) for r in 'CP'}
    oi = await cs.morning_oi_snapshot()
    class TK:
        def __init__(s, mp, l, c): s._mp = mp; s.last = l; s.close = c
        def marketPrice(s): return s._mp
    nan = float('nan'); sp = []
    for tk in (TK(nan, nan, 6701.5), TK(6702.0, nan, nan), TK(nan, nan, nan)):
        cs.spx_ticker = tk; sp.append(cs.spot())
    sp[2] = sp[2] is None
    return [sum(1 for v in oi.values() if v > 0), len(oi), any(ib.calls), ib.peak <= 5, ib.open, sp]
print(asyncio.run(go()))
"""
o = subprocess.run([sys.executable, "-c", code22], cwd=HERE, capture_output=True, text=True, timeout=60)
got = (o.stdout.strip().splitlines() or ["?"])[-1]
if got != "[24, 24, False, True, 0, [6701.5, 6702.0, True]]":
    r22.append("OI scan %r %s" % (got, o.stderr.strip()[-300:]))
src = open(os.path.join(HERE, "gex_bridge", "chain.py")).read()
if "snapshot=True" in src.split('"""', 2)[2]:
    r22.append("gex_bridge/chain.py still requests snapshot=True")
msrc = open(os.path.join(HERE, "gex_bridge", "main.py")).read()
if not (0 < msrc.find("chain.ensure_spx()") < msrc.find("_await_spot(chain)")):
    r22.append("SPX is not subscribed before the spot wait")
check("R22 bridge: OI scan by brief streaming, lines capped; SPX subscribed before spot wait; NaN-safe spot",
      not r22, "; ".join(r22))

# R23 bridge publish survives a reader holding levels.json; legal OPT ticks --
r23 = []
code23 = r"""
import sys, os, json, tempfile
sys.path.insert(0, 'gex_bridge')
import publish, config
d = tempfile.mkdtemp(); p = os.path.join(d, 'levels.json')
real = os.replace; n = {'k': 0}
def flaky(a, b):
    n['k'] += 1
    if n['k'] <= 3: raise PermissionError(5, 'Access is denied')
    real(a, b)
publish.os.replace = flaky
ok1 = publish.publish(p, {'a': 1})
publish.os.replace = lambda a, b: (_ for _ in ()).throw(PermissionError(5, 'denied'))
ok2 = publish.publish(p, {'a': 2})
left = [f for f in os.listdir(d) if f.endswith('.tmp')]
print([ok1, ok2, json.load(open(p))['a'], left, '107' in config.GENERIC_TICKS.split(',')])
"""
o = subprocess.run([sys.executable, "-c", code23], cwd=HERE, capture_output=True, text=True, timeout=60)
got = (o.stdout.strip().splitlines() or ["?"])[-1]
if got != "[True, False, 1, [], False]":
    r23.append("publish %r %s" % (got, o.stderr.strip()[-300:]))
check("R23 levels.json publish retries when a reader holds it (never raises); no tick 107",
      not r23, "; ".join(r23))

print("\n%d passed, %d failed" % (len(passes), len(fails)))
sys.exit(1 if fails else 0)

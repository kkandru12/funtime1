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

print("\n%d passed, %d failed" % (len(passes), len(fails)))
sys.exit(1 if fails else 0)

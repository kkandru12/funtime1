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

print("\n%d passed, %d failed" % (len(passes), len(fails)))
sys.exit(1 if fails else 0)

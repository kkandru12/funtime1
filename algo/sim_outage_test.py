#!/usr/bin/env python3
"""0DTE algo outage behaviour [v1.05 RESILIENT] -- dry run, fake IBKR + feed.
Called by ../regression_funtime.py (R13).  Exit 0 = pass.

  O1 IBKR drops mid-trade -> FATAL, CARRY (not flattened), run_session returns
     "retry"; the next session resumes the SAME position (CARRY_RESUMED) and
     manages it to its 10X-lock exit.
  O2 bridge/levels stale while holding -> EXITS_FROZEN, no exit on frozen
     quotes; once fresh again the position is managed normally.
  O3 IBKR down at start (SystemExit from connect) during the session -> main()
     retries in 60s instead of sleeping to the next session.
"""
import asyncio, os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["CRUSH_LOG_DIR"] = tempfile.mkdtemp()
import config  # noqa: E402
config.LOOP_CADENCE_SEC = 0
config.active_window = lambda n: ("B", 8, 12)
config.LOOP_END = (99, 0)
config.FLAT_TIME = (99, 0)
config.VEHICLE = "spread"
import main  # noqa: E402

src = open(os.path.join(HERE, "sim_spread_test.py")).read()
ns = {"__file__": os.path.join(HERE, "sim_spread_test.py")}
exec(src.split("async def _run(")[0].replace("import main       # noqa: E402", ""), ns)
ns["main"] = main
FakeIB, FakeLevels, cand, leg = ns["FakeIB"], ns["FakeLevels"], ns["cand"], ns["leg"]

fails = []
events = []


async def emit(ev, data): events.append((ev, data))


def names(): return [e for e, _ in events]


def setup(fl, drop_at=None):
    main.emit = emit
    main._shutdown = asyncio.Event()
    calls = {"n": 0}

    async def connect_ib(): return FakeIB(), "DU1234567"

    async def ensure_connected(ib):
        calls["n"] += 1
        return not (drop_at and calls["n"] == drop_at)
    main.connect_ib, main.ensure_connected = connect_ib, ensure_connected
    main.account_net_liq = lambda ib, a: 100000.0
    main.LevelsWatcher = lambda: fl


class _Args: log_dir = os.environ["CRUSH_LOG_DIR"]


entry = leg(0.35, 0.40, 0.10, 0.15)

# O1 ---------------------------------------------------------------------
script = [entry, entry, leg(3.7, 3.8, 0.05, 0.1)] + [leg(6.1, 6.2, .05, .1)] + \
         [leg(4.05, 4.1, .05, .05)] * 4
fl = FakeLevels([cand("6720C", 0.40)], script)
setup(fl, drop_at=4)                      # drop after entry + lock
res1 = asyncio.run(main.run_session(True, _Args()))
if res1 != "retry": fails.append("O1 run_session returned %r, want 'retry'" % res1)
if "CARRY" not in names(): fails.append("O1 no CARRY (position would be lost)")
if [d for e, d in events if e == "CLOSED"]:
    fails.append("O1 position was closed during the outage")
fl.cands = []                             # no new entries in session 2
setup(fl)
res2 = asyncio.run(main.run_session(True, _Args()))
if "CARRY_RESUMED" not in names(): fails.append("O1 position not resumed")
c = [d for e, d in events if e == "CLOSED"]
if not c or c[0]["reason"] != "10x-lock":
    fails.append("O1 resumed position not managed to 10x-lock: %s" % c[:1])

# O2 ---------------------------------------------------------------------
events.clear()
script = [entry, entry, leg(3.7, 3.8, .05, .1), leg(6.1, 6.2, .05, .1)] + \
         [leg(1.0, 1.1, .05, .05)] * 3 + [leg(6.1, 6.2, .05, .1)] + \
         [leg(4.05, 4.1, .05, .05)] * 4
fl = FakeLevels([cand("6720C", 0.40)], script)
orig_get = fl.get


def get_with_stale():
    lv = orig_get()
    fl.stale = 5 <= fl.n <= 7            # quotes collapse while data is stale
    return lv
fl.get = get_with_stale
setup(fl)
asyncio.run(main.run_session(True, _Args()))
if "EXITS_FROZEN" not in names(): fails.append("O2 no EXITS_FROZEN while stale")
c = [d for e, d in events if e == "CLOSED"]
if not c:
    fails.append("O2 position never closed after data came back")
elif [d for e, d in events if e == "SIM_EXIT"][0]["px"] < 3.0:
    fails.append("O2 sold on frozen/stale quotes: %s" % c[0])

# O3 ---------------------------------------------------------------------
events.clear()
n = {"c": 0}


async def run_session_fail(dry, args):
    n["c"] += 1
    if n["c"] == 1:
        raise SystemExit("could not connect to IBKR after 10 tries: refused")
    main._shutdown.set()
    return "done"
main.run_session = run_session_fail
slept = []


async def fake_sleep(s): slept.append(s)
main.sleep_interruptible = fake_sleep


async def fake_next(): slept.append("next-session")
main.sleep_until_next_session = fake_next
main._shutdown = asyncio.Event()
sys.argv = ["main.py", "--dry-run"]
asyncio.run(main.main())
if n["c"] < 2 or slept[:1] != [60]:
    fails.append("O3 connect failure did not retry in 60s (sleeps=%s runs=%d)" % (slept, n["c"]))

for f in fails:
    print("FAIL", f)
print("SIM OK" if not fails else "SIM FAILED (%d)" % len(fails))
sys.exit(1 if fails else 0)

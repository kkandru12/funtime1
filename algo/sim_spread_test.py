#!/usr/bin/env python3
"""Dry-run test of the 0DTE spread vehicle [v1.04 SPREAD10X].

Runs the REAL algo main.run_session() with a fake IBKR connection and a
fake levels.json feed (scripted quotes per loop), then checks the events.
Called by ../regression_funtime.py (R12).  Exit 0 = pass.

  S1 lock + trail  : debit 0.30 -> value 3.5 (10X lock) -> 6.0 -> 4.0
                     => CLOSED reason 10x-lock, exit >= 10 x debit
  S2 cap           : value runs to 9.60 => CLOSED spread-max
  S3 lock holds 10X: value 3.5 then 3.0  => CLOSED 10x-lock at 3.00 (= 10X)
  S4 too expensive : debit 1.30 (> $1.00, cannot pay 10X) => SPREAD_NONE, no entry
  S5 lowest debit  : two candidates (0.30 vs 0.15) => the 0.15 spread is bought
  S6 naked mode    : CRUSH_VEHICLE=naked => single-option SIM_ENTER (no spread)
  S7 size          : every entry is 10 contracts
  S8 no exit before 10X: value 0.30 -> 2.9 -> 0.5 => no close until 15:55 flat
"""
import asyncio, os, sys, tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["CRUSH_LOG_DIR"] = tempfile.mkdtemp()

import config     # noqa: E402
config.LOOP_CADENCE_SEC = 0
config.active_window = lambda n: ("B", 8, 12)
config.LOOP_END = (99, 0)
import risk       # noqa: E402
import main       # noqa: E402
import spreads    # noqa: E402


class FakeIB:
    def isConnected(self): return True
    def disconnect(self): pass
    async def qualifyContractsAsync(self, *c): return list(c)


class FakeLevels:
    def __init__(self, cands, script, flat_at=None):
        self.cands, self.script, self.n = cands, script, 0
        self.flat_at = flat_at
        self.q = {}
        self._advance()

    def _advance(self):
        # get() #1 is main's _await_ny_levels; loop k reads script[k-1]
        step = self.script[min(max(self.n - 1, 0), len(self.script) - 1)]
        self.q.update(step)

    def get(self):
        self.n += 1
        self._advance()
        if self.n > len(self.script) + 3:
            main._shutdown.set()
        if self.flat_at and self.n >= self.flat_at:
            config.FLAT_TIME = (0, 0)
        return {"session": "ny", "spx": 6710.0, "walls": {}}

    def entries_allowed(self, now): return True, "ok"
    def quote(self, k): return self.q.get(k, (None, None))
    def candidates(self): return self.cands
    def regime(self): return {"mode": "observe"}
    def load_contracts(self):
        return {k: dict(symbol="SPX", lastTradeDateOrContractMonth="20261002",
                        strike=float(k[:-1]), right=k[-1], exchange="SMART")
                for k in ("6720C", "6730C", "6725C", "6735C")}
    def age_sec(self): return 1.0


def cand(key, ask):
    return dict(key=key, strike=float(key[:-1]), right="call", ask=ask, bid=ask - 0.05,
                delta=0.1, gamma=0.01, score=1.0, ask_z=-1.5, window="B", otm=10,
                spot=6710.0, wall=None, trigger="crush")


def leg(lb, la, sb, sa, long_k="6720C", short_k="6730C"):
    return {long_k: (lb, la), short_k: (sb, sa)}


async def _run(fl, vehicle="spread"):
    events = []

    async def emit(ev, data): events.append((ev, data))
    main.emit = emit
    main._shutdown = asyncio.Event()
    config.VEHICLE = vehicle
    config.FLAT_TIME = (99, 0)

    async def connect_ib(): return FakeIB(), "DU1234567"
    async def ensure_connected(ib): return True
    main.connect_ib, main.ensure_connected = connect_ib, ensure_connected
    main.account_net_liq = lambda ib, a: 100000.0
    main.LevelsWatcher = lambda: fl

    class _Args: log_dir = os.environ["CRUSH_LOG_DIR"]
    await asyncio.wait_for(main.run_session(True, _Args()), timeout=120)
    return events


def run(fl, vehicle="spread"):
    return asyncio.run(_run(fl, vehicle))


def ev(evs, name): return [d for e, d in evs if e == name]


fails = []
entry = leg(0.35, 0.40, 0.10, 0.15)          # debit = 0.40 - 0.10 = 0.30

# S1 ---------------------------------------------------------------------
s1 = [entry, entry, leg(3.70, 3.8, 0.05, 0.20), leg(6.10, 6.2, 0.05, 0.10),
      leg(4.05, 4.1, 0.05, 0.05), leg(4.05, 4.1, 0.05, 0.05), leg(4.05, 4.1, 0.05, 0.05)]
e1 = run(FakeLevels([cand("6720C", 0.40)], s1))
c = ev(e1, "CLOSED")
if not c or c[0]["reason"] != "10x-lock":
    fails.append("S1 expected CLOSED 10x-lock, got %s" % c[:1])
else:
    x = [d for d in ev(e1, "SIM_EXIT")][0]["px"]
    if x < 0.30 * 10 - 1e-9:
        fails.append("S1 exit %.2f below 10X lock 3.00" % x)
if not ev(e1, "TRAIL_ARM"):
    fails.append("S1 no TRAIL_ARM (10X lock) event")

# S2 ---------------------------------------------------------------------
s2 = [entry, entry, leg(5, 5.1, 0.05, 0.1), leg(9.70, 9.8, 0.05, 0.10)]
e2 = run(FakeLevels([cand("6720C", 0.40)], s2))
c = ev(e2, "CLOSED")
if not c or c[0]["reason"] != "spread-max":
    fails.append("S2 expected CLOSED spread-max, got %s" % c[:1])

# S3 ---------------------------------------------------------------------
s3 = [entry, entry, leg(3.60, 3.7, 0.05, 0.10), leg(3.05, 3.1, 0.05, 0.05),
      leg(3.05, 3.1, 0.05, 0.05), leg(3.05, 3.1, 0.05, 0.05)]
e3 = run(FakeLevels([cand("6720C", 0.40)], s3))
c = ev(e3, "CLOSED")
if not c or c[0]["reason"] != "10x-lock":
    fails.append("S3 expected 10x-lock close at the floor, got %s" % c[:1])
elif ev(e3, "SIM_EXIT")[0]["px"] < 3.0 - 1e-9:
    fails.append("S3 exit below 10X: %s" % ev(e3, "SIM_EXIT")[0]["px"])

# S4 ---------------------------------------------------------------------
dear = leg(1.45, 1.50, 0.20, 0.25)            # debit 1.30 > 1.00
e4 = run(FakeLevels([cand("6720C", 1.50)], [dear] * 5))
if ev(e4, "SIM_ENTER"): fails.append("S4 entered a spread that cannot pay 10X")
if not ev(e4, "SPREAD_NONE"): fails.append("S4 no SPREAD_NONE event")

# S5 ---------------------------------------------------------------------
two = {**leg(0.35, 0.40, 0.10, 0.15),
       **leg(0.20, 0.25, 0.10, 0.12, "6725C", "6735C")}   # 0.30 vs 0.15
e5 = run(FakeLevels([cand("6720C", 0.40), cand("6725C", 0.25)], [two] * 5))
se = ev(e5, "SIM_ENTER")
if not se or se[0].get("key") != "6725C/6735C" or abs(se[0].get("debit", 0) - 0.15) > 1e-9:
    fails.append("S5 expected the 0.15 debit 6725C/6735C spread, got %s" % se[:1])

# S6 ---------------------------------------------------------------------
e6 = run(FakeLevels([cand("6720C", 0.40)], [entry] * 4), vehicle="naked")
se = ev(e6, "SIM_ENTER")
if not se or se[0].get("key") != "6720C" or se[0].get("vehicle") == "spread":
    fails.append("S6 naked mode should buy the single 6720C, got %s" % se[:1])

# S7 ---------------------------------------------------------------------
for name, evs in (("S1", e1), ("S2", e2), ("S5", e5), ("S6", e6)):
    for d in ev(evs, "SIM_ENTER"):
        if d.get("qty") != 10:
            fails.append("S7 %s qty %s != 10" % (name, d.get("qty")))

# S8 ---------------------------------------------------------------------
s8 = [entry, entry, leg(2.95, 3.0, 0.05, 0.10), leg(0.55, 0.6, 0.05, 0.05)] + \
     [leg(0.55, 0.6, 0.05, 0.05)] * 3
e8 = run(FakeLevels([cand("6720C", 0.40)], s8, flat_at=7))
c = ev(e8, "CLOSED")
if not c or c[0]["reason"] != "15:55-flat":
    fails.append("S8 expected only the 15:55 flat (no stop before 10X), got %s" % c[:1])

for f in fails:
    print("FAIL", f)
print("SIM OK" if not fails else "SIM FAILED (%d)" % len(fails))
sys.exit(1 if fails else 0)

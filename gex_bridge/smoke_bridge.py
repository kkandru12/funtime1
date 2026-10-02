#!/usr/bin/env python3
"""Smoke tests for the bridge architecture (bridge + two consumers).

Run: PYTHONPATH=/tmp/stubs python3 /tmp/smoke_bridge.py
Covers: atomic publish/consume, stale fail-safe, overnight confidence
decay, session detection, ES=SPX+basis, 0DTE screen, zone wall-break,
StableWall walls, BridgeGex adapter, PA sleeves C/D, py_compile of all
three packages.
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import importlib.util


def load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


WS = os.path.expanduser("~/workspace")
# bridge modules first, under their plain names ('import config' inside
# bridge modules must resolve to the BRIDGE config)
bconf = load_mod("bconf", f"{WS}/gex_bridge/config.py")
sys.modules["config"] = bconf
for _n in ["publish", "pacer", "ibkr_conn", "chain", "stable_gex", "screen",
           "regime"]:
    load_mod(_n, f"{WS}/gex_bridge/{_n}.py")
bmain = load_mod("bmain", f"{WS}/gex_bridge/main.py")
publish = sys.modules["publish"].publish
screen = sys.modules["screen"]
stable_gex = sys.modules["stable_gex"]

sys.path.insert(0, f"{WS}/algo")
from levels import LevelsWatcher

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" [{detail}]" if detail else ""))


# ============ 1. atomic publish/consume round-trip ============
tmpd = tempfile.mkdtemp()
lp = os.path.join(tmpd, "levels.json")
payload = {"ts_utc": "x", "session": "ny", "spx": 6500.0,
           "candidates": [{"key": "6560C", "ask": 0.35}]}
publish(lp, payload)
check("atomic-publish-readable", json.load(open(lp))["spx"] == 6500.0)
check("no-tmp-leftovers", not any(f.startswith(".levels-")
                                  for f in os.listdir(tmpd)))

w = LevelsWatcher(shared_dir=tmpd)
check("watcher-reads", w.get()["session"] == "ny")
check("watcher-fresh", w.fresh())
check("entries-allowed-fresh",
      w.entries_allowed(datetime.now(bconf.ET))[0] is True)

# ============ 2. stale fail-safe ============
old = time.time() - 120
os.utime(lp, (old, old))
w2 = LevelsWatcher(shared_dir=tmpd)
w2.refresh()
check("stale-detected", w2.age_sec() > 60)
ok, why = w2.entries_allowed(datetime.now(bconf.ET))
check("stale-blocks-entries", ok is False, why)
w3 = LevelsWatcher(shared_dir="/nonexistent")
ok, why = w3.entries_allowed(datetime.now(bconf.ET))
check("missing-blocks-entries", ok is False, why)

# ============ 3. session detection ============
ET = bconf.ET
check("sess-ny", bconf.session(datetime(2026, 10, 1, 10, 0, tzinfo=ET)) == "ny")
check("sess-overnight",
      bconf.session(datetime(2026, 10, 1, 20, 0, tzinfo=ET)) == "overnight")
check("sess-sat",
      bconf.session(datetime(2026, 10, 3, 12, 0, tzinfo=ET)) == "weekend")
check("sess-fri-eve",
      bconf.session(datetime(2026, 10, 2, 18, 0, tzinfo=ET)) == "weekend")
check("sess-sun-eve",
      bconf.session(datetime(2026, 10, 4, 18, 0, tzinfo=ET)) == "overnight")

# ============ 4. ES = SPX + basis, overnight decay ============
walls = {"call": {"strike": 6550, "zone_lo": 6545, "zone_hi": 6555,
                  "confidence": 0.8, "tenure_min": 30, "dominance": 3.0},
         "put": {"strike": 6450, "zone_lo": 6445, "zone_hi": 6455,
                 "confidence": 0.6, "tenure_min": 30, "dominance": 2.8}}
esw = bmain._es_walls(walls, 20.4)
check("es-call-strike", esw["call"]["strike"] == 6570.5, esw["call"]["strike"])
check("es-put-zone", esw["put"]["zone_lo"] == 6465.5, esw["put"]["zone_lo"])
check("es-quarter-grid", esw["call"]["strike"] % 0.25 == 0)

state = {"frozen": {"call": walls["call"], "put": walls["put"],
                    "flip": 6500, "regime": "+", "net_gex_b": 1.2,
                    "magnets": [6600]},
         "wall_ts": time.time() - 120 * 60, "basis": 20.0,
         "wall_ts_utc": "t", "basis_ts_utc": "t",
         "regime": type("R", (), {"score": None, "detail": {}})(),
         "chain": None, "es_ticker": None, "oi_ts_utc": "t"}
p = bmain.build_payload(state, datetime.now(ET), "overnight")
check("overnight-stale-flag", p["stale"] is True)
check("overnight-wall-age", abs(p["wall_age_min"] - 120) < 1, p["wall_age_min"])
# decay: 120min -> x max(0.3, 1-120/240)=0.5
check("overnight-decay", abs(p["walls"]["call"]["confidence"] - 0.4) < 0.01,
      p["walls"]["call"]["confidence"])
check("overnight-no-candidates", p["candidates"] == [])
check("overnight-es-walls", p["es_walls"]["call"]["strike"] == 6570.0)

state2 = dict(state, wall_ts=time.time() - 600 * 60)  # 10h -> floor 0.3
p2 = bmain.build_payload(state2, datetime.now(ET), "overnight")
check("decay-floor", abs(p2["walls"]["call"]["confidence"] - 0.24) < 0.01,
      p2["walls"]["call"]["confidence"])

# ============ 5. 0DTE screen (bridge/screen.py) ============


class FakeChain:
    def iv_ok(self):
        return True, "ok"

    def ask_zscore(self, key):
        return -1.5


class FakeGex:
    call_wall = 6560
    put_wall = 6440
    flip = 6500
    magnets = [6600]


def uni(strikes=(6560,), rights=("call",)):
    out = []
    for s in strikes:
        for r in rights:
            rr = "call" if r == "call" else "put"
            out.append(dict(key=(s, "C" if rr == "call" else "P"), strike=s,
                            right=rr, ask=0.35, bid=0.30, mid=0.325,
                            delta=0.05, gamma=0.004, iv=0.12))
    return out


now_a = datetime(2026, 10, 1, 11, 30, tzinfo=ET)  # window A
cands, rej, notes = screen.evaluate(uni(), FakeChain(), FakeGex(), now_a, 6510.0)
check("screen-candidate", len(cands) == 1 and cands[0]["key"] == "6560C",
      cands[0] if cands else None)
check("screen-trigger", cands[0]["trigger"] == "crush")
check("screen-json-key", isinstance(cands[0]["key"], str))
now_out = datetime(2026, 10, 1, 13, 0, tzinfo=ET)  # dead zone
cands, _, _ = screen.evaluate(uni(), FakeChain(), FakeGex(), now_out, 6510.0)
check("screen-dead-zone-empty", cands == [])

# ============ 6. zone wall-break (bridge) ============
wb = screen.WallBreakState()


class FakeGexW:
    call_wall = 6560
    call_zone = (6555, 6565)
    put_wall = None
    put_zone = None


oi = {(6560.0, "C"): 25000, (6555.0, "C"): 8000}
evs = wb.note(datetime(2026, 10, 1, 11, 40, tzinfo=ET), 6553.0, FakeGexW(), oi)
check("wb-arm-zone", wb.armed is not None and wb.armed["zone"] == (6555, 6565),
      wb.armed)
# 2 M1 closes above zone_hi -> break
t0 = datetime(2026, 10, 1, 11, 41, tzinfo=ET)
for m, px in ((41, 6553.0), (42, 6567.0), (43, 6568.0), (44, 6569.0)):
    wb.note(t0.replace(minute=m), px, FakeGexW(), oi)
sig = wb.break_signal()
check("wb-break-2closes", sig == "up", sig)
cand, _, _ = screen.evaluate_wallbreak(uni((6570,), ("call",)), 6568.0, wb)
check("wb-candidate", cand is not None and cand["trigger"] == "wallbreak",
      cand["key"] if cand else None)
check("wb-disarmed-after", wb.armed is None)

# ============ 7. StableWall (bridge/stable_gex.py) ============
sg = stable_gex.GexState()
base = time.time()
gm = {(float(k), "C"): 0.002 + 0.0001 * (k % 7) for k in range(6400, 6620, 5)}
gm.update({(float(k), "P"): 0.002 for k in range(6400, 6620, 5)})
gm[(6560.0, "C")] = 0.02  # dominant call gamma
gm[(6440.0, "P")] = 0.02  # dominant put gamma
oi2 = {(float(k), r): 10000.0 for k in range(6400, 6620, 5)
       for r in ("C", "P")}
sg.note_gamma(gm, base)
ev = sg.maybe_evaluate(oi2, 6500.0, base)
p7 = sg.walls_payload(base)
check("stablewall-eval", ev is not None)
check("stablewall-call", p7["call_wall"] == 6560.0, p7["call_wall"])
check("stablewall-put", p7["put_wall"] == 6440.0, p7["put_wall"])
check("stablewall-zones", p7["call_zone"] is not None and p7["put_zone"] is not None)
check("stablewall-regime", p7["regime"] in ("+", "-", "flat"), p7["regime"])

# ============ 8. BridgeGex adapter (algo_es) ============
# algo_es modules do 'import config' -> point it at the ALGO_ES config now
# (bridge imports above are already fully loaded)
esconf = load_mod("esconf", f"{WS}/algo_es/config.py")
sys.modules["config"] = esconf
sys.path.insert(0, f"{WS}/algo_es")
import strategy as esstrat
from bridge_levels import LevelsWatcher as ESLW
from bridge_gex import BridgeGex

publish(lp, {"ts_utc": "x", "session": "ny", "spx": 6500.0, "es": 6520.4,
             "basis": 20.4, "basis_ts_utc": "x",
             "walls": {"call": {"strike": 6550, "zone_lo": 6545,
                                "zone_hi": 6555, "confidence": 0.72,
                                "tenure_min": 40, "dominance": 3.1},
                       "put": {"strike": 6450, "zone_lo": 6445,
                               "zone_hi": 6455, "confidence": 0.65,
                               "tenure_min": 40, "dominance": 2.8}},
             "es_walls": None, "flip": 6480, "regime": "+",
             "net_gex_b": 1.2, "magnets": [6600],
             "wall_ts_utc": "x", "stale": False, "wall_age_min": 0.0,
             "candidates": [], "chain_frame": {}, "oi_ts_utc": "x",
             "regime_info": {}})
# fill es_walls like the bridge would
doc = json.load(open(lp))
doc["es_walls"] = bmain._es_walls(doc["walls"], 20.4)
json.dump(doc, open(lp, "w"))

elw = ESLW(shared_dir=tmpd)
bg = BridgeGex(elw)
check("bridgegex-refresh", bg.refresh() is True)
check("bridgegex-call-es", bg.call_wall == 6570.5, bg.call_wall)
check("bridgegex-zone-es", bg.call_zone == (6565.5, 6575.5), bg.call_zone)
check("bridgegex-regime", bg.gamma_regime() == "+")
wall, side, edge, conf = bg.wall_for_fade(6564.0)
check("bridgegex-fade-touch",
      wall == 6570.5 and side == "short" and edge == 6565.5, (wall, side, edge))
check("bridgegex-fade-miss", bg.wall_for_fade(6500.0)[0] is None)
check("bridgegex-magnet", bg.magnet_beyond(6560.0, 1) == 6620.4, bg.magnet_beyond(6560.0, 1))
check("bridgegex-dominance", bg.dominance(6570.5, "C", {}) == 3.1)
check("bridgegex-flip-es", bg.flip == 6500.4, bg.flip)

# ES WallBreakState with adapter (min_conf override)
wb2 = esstrat.WallBreakState()
evs = wb2.note(6564.0, bg, {}, min_conf=0.5)
check("es-wb-arm", wb2.armed is not None, wb2.armed)

# ============ 9. PA sleeves (synthetic MT5 bars) ============
pa = esstrat.OvernightPA()
sess_start = datetime(2026, 10, 1, 18, 0, tzinfo=ET)
pa.reset(sess_start)
bars = []
t = sess_start
px = 6500.0
for i in range(120):  # 2h of bars, range 6495..6505 (10pt -> tradeable)
    hi = 6505.0 if i % 2 == 0 else 6504.0
    lo = 6495.0 if i % 3 == 0 else 6496.0
    bars.append((t, px, hi, lo, px))
    t += timedelta(minutes=1)
pa.note_bars(bars)
check("pa-range", pa.on_high == 6505.0 and pa.on_low == 6495.0,
      (pa.on_high, pa.on_low))
now_on = datetime(2026, 10, 1, 22, 0, tzinfo=ET)
c = pa.evaluate_c(now_on, 6495.5)  # touch of ON low
check("pa-fade-signal", c is not None and c["trigger"] == "pa_fade", c)
# sweep: exceed low by >=2 then reclaim within 15 min
bars2 = list(bars)
t2 = sess_start + timedelta(minutes=120)
bars2.append((t2, 6492.0, 6493.0, 6491.0, 6492.5))      # sweep low
bars2.append((t2 + timedelta(minutes=5), 6496.0, 6497.0, 6495.5, 6496.5))  # reclaim
pa.note_bars(bars2)
d = pa.evaluate_d(sess_start + timedelta(minutes=126), 6496.5)
check("pa-sweep-signal", d is not None and d["trigger"] == "pa_sweep", d)

# ============ 10. py_compile everything ============
import py_compile
roots = [os.path.expanduser("~/workspace/gex_bridge"),
         os.path.expanduser("~/workspace/algo"),
         os.path.expanduser("~/workspace/algo_es")]
bad = []
for r in roots:
    for f in os.listdir(r):
        if f.endswith(".py"):
            try:
                py_compile.compile(os.path.join(r, f), doraise=True)
            except Exception as e:
                bad.append((f, str(e)))
check("py-compile-all", not bad, bad)

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)

#!/usr/bin/env python3
"""End-to-end dry-run test of the Globex sleeve wiring [v1.02 GLOBEXWIRE].

Runs the REAL main.run_session() against a fake MetaTrader5 module (no
terminal, no network, no orders) and a stub strategy, then checks the event
stream.  Called by ../regression_funtime.py (R10).  Exit 0 = pass.

Scenarios
  A  stub fires BUY (stop below, target above)  -> GLOBEX_SIGNAL, CANDIDATE,
     SIM_ENTER trigger globex_smacross, then price rallies -> CLOSED target-2
  B  stub fires BUY with the stop ABOVE price    -> GLOBEX_REJECT, no entry
  C  real strategies (no stub) on 30 bars       -> loop runs, no exception
  F  [v1.09 NATIVECLOSE] LIVE path (fake order_send / deals): the broker
     closes the trade at a SLIPPED stop (not within 2 ticks) -> CLOSED
     native-exit with the loss booked, and a later signal can enter again
  G  [v1.09 CLOSEGUARD] the broker already closed the trade but the bot does
     not know -> at session end NO close order is sent (CLOSE_SKIPPED)
  D  [v1.05 RESILIENT] MT5 quotes vanish mid-session -> MT5_RECONNECT, the
     feed reconnects (MT5_RECONNECTED), quotes return and the loop goes on
     (no crash, no exit); IBKR/bridge down (no levels.json) never stops the
     Globex sleeve -- it already runs with gex=None in A-C
"""
import asyncio, os, sys, tempfile, types
from datetime import datetime, timedelta, timezone
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
for k, v in {"MT5_PATH": "x", "MT5_SERVER": "demo", "MT5_LOGIN": "1",
             "MT5_PASSWORD": "x", "MT5_SYMBOL": "ESZ6",
             "ES_LOG_DIR": tempfile.mkdtemp(), "SHARED_DIR": tempfile.mkdtemp(),
             "ES_GLOBEX_REFRESH_SEC": "0"}.items():
    os.environ[k] = v


# ------------------------------------------------------------ fake MT5 ---
class FakeMT5(types.ModuleType):
    TIMEFRAME_M1, TIMEFRAME_M15, TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1 = 1, 15, 60, 240, 1440
    SYMBOL_TRADE_MODE_FULL = 4
    ORDER_FILLING_IOC, ORDER_FILLING_FOK, ORDER_FILLING_RETURN = 2, 1, 0
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TIME_GTC = 0, 1, 0
    TRADE_RETCODE_DONE, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT = 10009, 1, 2

    def __init__(self):
        super().__init__("MetaTrader5")
        self.minute = 0          # advances one M1 bar per loop
        self.px = 7700.0
        self.path = None         # optional fn(minute) -> price
        self.max_loops = 40
        self.stop_event = None
        self.outage = None
        self.inits = 0

    def initialize(self, path=None):
        self.inits += 1
        return True
    def login(self, *a, **k): return True
    def last_error(self): return (0, "ok")
    def shutdown(self): pass
    def symbol_select(self, *a): return True
    def symbols_get(self, pat=None): return ()
    def terminal_info(self): return types.SimpleNamespace(trade_allowed=True)
    def account_info(self): return types.SimpleNamespace(balance=100000.0)
    def symbol_info(self, s):
        return types.SimpleNamespace(trade_mode=4, visible=True, filling_mode=2,
                                     expiration_time=None, name=s)

    def symbol_info_tick(self, s):
        self.minute += 1
        if self.outage and self.outage[0] <= self.minute < self.outage[1]:
            if self.minute >= self.max_loops and self.stop_event:
                self.stop_event.set()
            return None                      # terminal down: no tick
        if self.path:
            self.px = self.path(self.minute)
        if self.minute >= self.max_loops and self.stop_event:
            self.stop_event.set()
        return types.SimpleNamespace(bid=self.px - 0.125, ask=self.px + 0.125)

    def copy_rates_from_pos(self, sym, tf, start, n):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        step = timedelta(minutes=tf)
        dt = np.dtype([("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
                       ("close", "f8"), ("tick_volume", "i8")])
        rows = []
        # bar times on the timeframe grid: a D1 bar only changes once a day
        cur = now + timedelta(minutes=self.minute)
        base = datetime.fromtimestamp((int(cur.timestamp()) // (tf * 60)) * tf * 60, timezone.utc)
        for i in range(n):
            t = base - step * (n - 1 - i)
            p = self.px - (n - 1 - i) * 0.01
            rows.append((int(t.timestamp()), p, p + 1, p - 1, p, 100))
        return np.array(rows, dtype=dt)


fake = FakeMT5()
sys.modules["MetaTrader5"] = fake

import config            # noqa: E402
config.in_trading_session = lambda n: True
config.is_pa_overnight = lambda n: False
config.past_flatten_time = lambda n: False
config.LOOP_CADENCE_SEC = 0
config.GLOBEX_REFRESH_SEC = 0
import strategies        # noqa: E402
import main              # noqa: E402
from strategies.signal import Signal   # noqa: E402


def stub_class(stop_off, tgt_off, fire_at=3):
    class Stub:
        def __init__(self, cfg=None):
            self.n = 0
        def on_bar(self, bar, state):
            self.n += 1
            if self.n == fire_at:
                p = state["last_price"]
                return Signal("buy", p, p + stop_off, p + tgt_off, "SMA_CROSS", "stub")
            return None
    return Stub


async def run(strats, path=None, loops=40):
    events = []

    async def emit(ev, data):
        events.append((ev, data))
    main.emit = emit
    main._shutdown = asyncio.Event()
    fake.minute, fake.px, fake.path, fake.max_loops = 0, 7700.0, path, loops
    fake.stop_event = main._shutdown
    os.environ["ES_GLOBEX_STRATEGIES"] = strats
    config.GLOBEX_STRATEGIES = strats
    await asyncio.wait_for(main.run_session(dry_run=True), timeout=60)
    return events


def names(evs):
    return [e for e, _ in evs]


fails = []
real_sma = strategies.STRATEGIES["smacross"]

# A ------------------------------------------------------------------------
strategies.STRATEGIES["smacross"] = stub_class(-10, +20)
import globex as _g      # noqa: E402
_g.STRATEGIES = strategies.STRATEGIES
evA = asyncio.run(run("smacross", path=lambda m: 7700.0 if m < 8 else 7730.0))
n = names(evA)
ent = [d for e, d in evA if e == "SIM_ENTER"]
closed = [d for e, d in evA if e == "CLOSED"]
if "GLOBEX_SLEEVE" not in n: fails.append("A: no GLOBEX_SLEEVE event")
if "GLOBEX_SIGNAL" not in n: fails.append("A: no GLOBEX_SIGNAL")
if not ent or ent[0].get("trigger") != "globex_smacross":
    fails.append("A: no SIM_ENTER globex_smacross (%s)" % ent[:1])
if not closed or closed[0].get("reason") != "target-2" or closed[0].get("pnl", 0) <= 0:
    fails.append("A: expected CLOSED target-2 with profit, got %s" % closed[:1])

st_es = os.path.join(os.environ["ES_LOG_DIR"], "status_es.json")
if not os.path.exists(st_es):
    fails.append("A: status_es.json not written (dashboard)")
else:
    import json as _json
    if "position" not in _json.load(open(st_es)):
        fails.append("A: status_es.json has no position field")

# B ------------------------------------------------------------------------
strategies.STRATEGIES["smacross"] = stub_class(+5, +20)   # stop ABOVE a long
evB = asyncio.run(run("smacross", loops=12))
if "GLOBEX_REJECT" not in names(evB): fails.append("B: no GLOBEX_REJECT")
if "SIM_ENTER" in names(evB): fails.append("B: entered a trade with a bad stop")

# C ------------------------------------------------------------------------
strategies.STRATEGIES["smacross"] = real_sma
evC = asyncio.run(run("vob,squeeze,bb2c,dma520,smacross,fivedma", loops=30))
if "GLOBEX_SLEEVE" not in names(evC) or "SESSION_LOOP_END" not in names(evC):
    fails.append("C: real strategies loop did not run to the end: %s" % names(evC)[-5:])
sleeve = [d for e, d in evC if e == "GLOBEX_SLEEVE"]
if sleeve and len(sleeve[0]["strategies"]) != 6:
    fails.append("C: expected 6 strategies (bb2c H4 only), got %s" % sleeve[0]["strategies"])

# E [v1.06 D1WARM] restart must not fire 5DMA-STRUCT on old daily bars ------
import strategies.fivedma_struct as fds
class AlwaysFire(fds.FiveDMAStruct):
    def on_bar(self, bar):
        super().on_bar(bar)
        return Signal("short", bar.close, bar.close + 10, bar.close - 20, "5DMA_STRUCT", "stub")
real5 = _g.FiveDMAStruct
_g.FiveDMAStruct = AlwaysFire
config.FIVEDMA_ENTRY = "daily"      # E is about the daily-close path's warm-up
evE = asyncio.run(run("fivedma", loops=10))
config.FIVEDMA_ENTRY = "m1"
sigE = [d for e, d in evE if e == "GLOBEX_SIGNAL"]
if sigE: fails.append("E: 5DMA-STRUCT fired on startup from old daily bars: %s" % sigE[:1])
_g.FiveDMAStruct = real5

# F / G: live path -----------------------------------------------------------
import types as _types
class LiveBits:
    def __init__(self): self.reset()
    def reset(self, slip_close_at=None, deal=True):
        self.open = {}; self.sent = []; self.ticket = 1000
        self.slip_close_at = slip_close_at; self.deal = deal; self.deals = []
LB = LiveBits()
def _order_send(req):
    LB.sent.append(dict(req))
    if req.get("position_id"):                       # a close
        LB.open.pop(req["position_id"], None)
        return _types.SimpleNamespace(retcode=10009, volume=req["volume"], price=req["price"], comment="ok")
    LB.ticket += 1
    LB.open[LB.ticket] = _types.SimpleNamespace(ticket=LB.ticket, magic=req["magic"], symbol=req["symbol"],
                                                type=req["type"], sl=req["sl"], tp=req["tp"])
    return _types.SimpleNamespace(retcode=10009, volume=req["volume"], price=req["price"], comment="ok")
def _positions_get(symbol=None, ticket=None):
    if ticket is not None:
        return tuple(p for t, p in LB.open.items() if t == ticket)
    return tuple(LB.open.values())
def _history_deals_get(a, b):
    return tuple(LB.deals)
def _tick_hook():
    # broker closes the open trade at a slipped price once the minute is reached
    if LB.slip_close_at and fake.minute >= LB.slip_close_at and LB.open:
        t, p = next(iter(LB.open.items()))
        LB.open.pop(t)
        if LB.deal:
            LB.deals.append(_types.SimpleNamespace(position_id=t, ticket=900 + len(LB.deals),
                            entry=fake.DEAL_ENTRY_OUT, symbol=p.symbol, volume=1.0, price=p.sl - 1.5))
        LB.slip_close_at = None
fake.order_send = _order_send
fake.positions_get = _positions_get
fake.history_deals_get = _history_deals_get
_orig_tick = FakeMT5.symbol_info_tick
def _tick(self, s):
    r = _orig_tick(self, s); _tick_hook(); return r
FakeMT5.symbol_info_tick = _tick

def stub_twice():
    class Stub:
        def __init__(self, cfg=None): self.n = 0
        def on_bar(self, bar, state):
            self.n += 1
            if self.n in (3, 15):
                p = state["last_price"]
                return Signal("buy", p, p - 10, p + 20, "SMA_CROSS", "stub")
            return None
    return Stub

async def run_live(strats, loops):
    events = []
    async def emit(ev, data): events.append((ev, data))
    main.emit = emit
    main._shutdown = asyncio.Event()
    fake.minute, fake.px, fake.path, fake.max_loops = 0, 7700.0, None, loops
    fake.stop_event = main._shutdown
    config.GLOBEX_STRATEGIES = strats
    await asyncio.wait_for(main.run_session(dry_run=False), timeout=60)
    return events

strategies.STRATEGIES["smacross"] = stub_twice()
_g.STRATEGIES = strategies.STRATEGIES
LB.reset(slip_close_at=8, deal=True)
evF = asyncio.run(run_live("smacross", 30))
nF = names(evF)
cF = [d for e, d in evF if e == "CLOSED"]
if not any(d.get("reason") == "native-exit" and d.get("pnl", 0) < 0 for d in cF):
    fails.append("F: slipped broker stop not booked as CLOSED native-exit: %s" % cF[:2])
if nF.count("ENTER") < 2:
    fails.append("F: bot stayed stuck after the broker close (entries: %d)" % nF.count("ENTER"))

strategies.STRATEGIES["smacross"] = stub_class(-10, +20)
_g.STRATEGIES = strategies.STRATEGIES
LB.reset(slip_close_at=8, deal=False)              # broker closed it, no deal seen
evG = asyncio.run(run_live("smacross", 14))
closes_sent = [r for r in LB.sent if r.get("position_id")]
if closes_sent:
    fails.append("G: a close order was sent for a trade the broker had already closed: %s" % closes_sent)
if "CLOSE_SKIPPED" not in names(evG):
    fails.append("G: no CLOSE_SKIPPED event")
strategies.STRATEGIES["smacross"] = real_sma
_g.STRATEGIES = strategies.STRATEGIES
FakeMT5.symbol_info_tick = _orig_tick

# D ------------------------------------------------------------------------
config.MT5_STALE_SEC = 0
fake.outage = (5, 12)
inits0 = fake.inits
evD = asyncio.run(run("vob,bb2c,dma520,smacross,fivedma", loops=25))
nD = names(evD)
if "MT5_RECONNECT" not in nD: fails.append("D: no MT5_RECONNECT during the outage")
if "MT5_RECONNECTED" not in nD: fails.append("D: feed never reconnected")
if fake.inits <= inits0 + 2: fails.append("D: MT5 initialize() not called again")
if "SESSION_LOOP_END" not in nD: fails.append("D: loop did not survive the outage")
fake.outage = None

for f in fails:
    print("FAIL", f)
print("SIM OK" if not fails else "SIM FAILED (%d)" % len(fails))
sys.exit(1 if fails else 0)

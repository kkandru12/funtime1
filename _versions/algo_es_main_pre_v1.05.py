"""ES futures GEX algo - always-on main loop (CONSUMER mode).

The bridge (gex_bridge/) is the account's single IBKR streaming connection.
This process holds ZERO IBKR market-data lines and makes ZERO IBKR calls:
  - GEX walls/zones/confidence/regime/flip + basis: ../shared/levels.json
    (atomic publish; read-only LevelsWatcher + BridgeGex adapter)
  - entry trigger prices + PA bars: the AMP MT5 terminal (same machine)
  - execution: MT5Executor -> MT5 demo account

Sleeve schedule (local clock is the authority; bridge session = cross-check):
  NY (09:30-16:00 ET):  fade + breakout off bridge levels (spot from MT5)
  overnight (18:00-09:00): breakouts on frozen bridge zones (half size,
      confidence bar +0.15) + PA sleeves C (range fade) + D (sweep+reclaim)
  halt 17:00-18:00 / weekend: flat, sleep.

Globex: Sun 18:00 -> Fri 17:00 ET. Always-on: sleeps until the next session
instead of exiting. Ctrl+C / SIGTERM always exits promptly (flatten first).
Pass --oneshot to run a single session then exit (debugging).

Globex sleeve [v1.02 GLOBEXWIRE]: the ported Apex strategies in
strategies/ (VOB, SQUEEZE, BB-2C, DMA-520, SMA-CROSS, 5DMA-STRUCT) run via
globex.py on every closed MT5 M1 bar, all sessions. They enter only when the
GEX sleeves have no candidate, the account is flat and the risk gates pass.

Default is DRY-RUN (no orders). Pass --live to transmit to the MT5 demo
account (--live = MT5 demo only; there is no live-money path).
"""
import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from bridge_levels import LevelsWatcher
from bridge_gex import BridgeGex
from strategy import (evaluate_fade, evaluate_breakout, WallBreakState,
                      Position, TP1_FILL, TP2_FILL, STOP, FLATTEN,
                      OvernightPA, true_risk_dollars)
from mt5_data import MT5DataFeed
from mt5_exec import MT5Executor
from risk import RiskManager
from globex import GlobexSleeve

ET = ZoneInfo("America/New_York")
log = logging.getLogger("algo_es")

_shutdown = asyncio.Event()
_log_handlers: list = []


def setup_logging(log_dir: str):
    """Per-session log setup: new dated file, previous handlers removed."""
    global _log_handlers
    root = logging.getLogger()
    for h in _log_handlers:
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    _log_handlers = []
    os.makedirs(log_dir, exist_ok=True)
    day = config.session_date(datetime.now(ET)).strftime("%Y%m%d")
    path = os.path.join(log_dir, f"algo_es_{day}.jsonl")
    fh = logging.FileHandler(path)
    fh.setFormatter(logging.Formatter("%(message)s"))
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.WARNING)
    root.addHandler(ch)
    _log_handlers = [fh, ch]
    return path


async def emit(event: str, data: dict):
    rec = {"ts": datetime.now(ET).isoformat(), "event": event, **data}
    logging.getLogger("algo_es").info(json.dumps(rec))


async def sleep_interruptible(total_secs: float):
    end = asyncio.get_event_loop().time() + total_secs
    while not _shutdown.is_set():
        remaining = end - asyncio.get_event_loop().time()
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(_shutdown.wait(),
                                   timeout=min(60.0, remaining))
        except asyncio.TimeoutError:
            pass


async def sleep_until_next_session():
    now = datetime.now(ET)
    target = config.next_session_start(now)
    secs = (target - now).total_seconds()
    if secs <= 0:
        return
    await emit("SLEEP", {"until": target.isoformat(), "secs": int(secs)})
    print(f"sleeping {int(secs)}s until next session "
          f"{target.strftime('%a %H:%M %Z')}", flush=True)
    await sleep_interruptible(secs)


async def run_session(dry_run: bool):
    """One Globex session. Returns when the session ends (halt/weekend)."""
    log_path = setup_logging(config.LOG_DIR)
    print(f"logging to {log_path} | mode={'DRY-RUN' if dry_run else 'LIVE-MT5-DEMO'}",
          flush=True)
    await emit("SESSION_START", {"mode": "dry-run" if dry_run else "live-mt5-demo"})

    # ---- bridge levels (read-only; the bridge owns all IBKR streaming) ----
    levels = LevelsWatcher()
    gex = BridgeGex(levels)
    gex_ok, gex_why = levels.gex_ok()
    if gex_ok:
        gex.refresh()
    await emit("LEVELS_STATUS", {"ok": gex_ok, "why": gex_why,
                                 "session": levels.session(),
                                 "age_s": round(levels.age_sec(), 1)})

    # ---- MT5 data feed: the ONLY quote source now (required) ----
    mt5data = MT5DataFeed(emit)
    try:
        await mt5data.connect()
    except Exception as e:  # noqa: BLE001 - without MT5 there is no market
        log.error("MT5 data feed failed: %s", e)
        await emit("MT5_DATA_DOWN", {"error": str(e)[:200],
                                     "note": "no quotes possible; session aborted"})
        return

    # ---- MT5 executor (demo account) ----
    om = MT5Executor(None, "MT5-DEMO", dry_run, mt5data.quote, emit)
    try:
        await om.connect()
    except Exception as e:  # noqa: BLE001
        log.error("MT5 executor connect failed: %s", e)
        await emit("MT5_EXEC_DOWN", {"error": str(e)[:200]})
        mt5data.shutdown()
        return

    try:
        acct = await asyncio.to_thread(om.mt5.account_info)
        acct_val = (acct.balance if acct and acct.balance
                    else config.ACCOUNT_FALLBACK)
    except Exception:
        acct_val = config.ACCOUNT_FALLBACK
    risk = RiskManager(acct_val)
    risk.new_day(config.session_date(datetime.now(ET)))
    await emit("RISK", {"net_liq": acct_val,
                        "daily_stop": round(risk.daily_stop_amt, 2),
                        "risk_per_trade": config.RISK_PER_TRADE})

    wb = WallBreakState()
    pa = OvernightPA()
    glx = GlobexSleeve(emit) if config.GLOBEX_ENABLED else None
    if glx:
        await glx.refresh(mt5data, force=True)
        await emit("GLOBEX_SLEEVE", {"strategies": list(glx.strats),
                                     "all_hours": config.GLOBEX_ALL_HOURS,
                                     "qty": config.GLOBEX_QTY})
    closes: deque = deque(maxlen=5)   # ES M1 closes from MT5 bars (breakout)
    position: Position | None = None
    pending_entry = False
    touches: dict[int, int] = {}      # fade touches per wall per day
    last_beat = datetime.now(ET)
    sess_mismatch_warned = False
    last_gex_ok = True

    await emit("RUN", {"note": "entering session loop (consumer mode)"})
    while not _shutdown.is_set():
        now = datetime.now(ET)
        if not config.in_trading_session(now):
            await emit("SESSION_END", {"reason": "halt-or-weekend"})
            break

        risk.new_day(config.session_date(now))

        # ---- spot: MT5 live quote (entry trigger price source) ----
        q = mt5data.quote()
        if not q:
            await asyncio.sleep(config.LOOP_CADENCE_SEC)
            continue
        bid, ask = q
        spot = (bid + ask) / 2

        # ---- MT5 M1 bars -> PA sleeves + breakout M1 closes ----
        await mt5data.sync()
        bars = mt5data.bars_since(
            pa.sess_start or config.overnight_session_start(now))
        pa.note_bars(bars)
        closes.clear()
        for b in list(mt5data.bars)[-5:]:
            closes.append(b[4])   # M1 bar closes for the breakout sleeve

        # ---- bridge levels ----
        gex_ok, gex_why = levels.gex_ok()
        if gex_ok:
            gex.refresh()

        # ---- Globex sleeve: every strategy sees every closed M1 bar ----
        glx_sigs = []
        if glx:
            await glx.refresh(mt5data)
            glx_sigs = glx.step(now, spot, gex if gex_ok else None)
            for gs in glx_sigs:
                sg = gs["signal"]
                await emit("GLOBEX_SIGNAL", {
                    "strategy": gs["name"], "side": sg.side,
                    "entry": round(float(sg.entry_px), 2),
                    "stop": round(float(sg.stop_px), 2),
                    "target": round(float(sg.target_px), 2),
                    "reason": str(getattr(sg, "reason", ""))[:160],
                    "flat": position is None})
        overnight = config.is_pa_overnight(now)   # local clock = authority
        bridge_sess = levels.session()
        if not sess_mismatch_warned and bridge_sess:
            expect = "overnight" if overnight else "ny"
            # bridge publishes "ny"/"overnight"; anything else is a mismatch
            if bridge_sess not in (expect,):
                sess_mismatch_warned = True
                await emit("BRIDGE_SESSION_MISMATCH",
                           {"bridge": bridge_sess, "local": expect,
                            "note": "local clock is the authority"})

        # ---- wall-break arming (reads bridge walls/zones) ----
        if gex_ok and spot:
            min_conf = config.BREAKOUT_MIN_CONFIDENCE + \
                (config.OVERNIGHT_CONF_BUMP if overnight else 0.0)
            for ev, info in wb.note(spot, gex, {}, min_conf):
                await emit("WB_" + ev.upper(), info)

        flatten_now = (config.past_flatten_time(now)
                       and config.FLATTEN_BEFORE_HALT)

        # ---- position management ----
        held_trigger = position.trigger if position else None
        if position and spot:
            if position.simulated:
                action = position.update(now, bid, ask,
                                         flatten=flatten_now)
                if action == TP1_FILL:
                    pnl = position.on_fill(position.tp1_qty,
                                           position.tp1_px, "tp1")
                    await emit("TP1", {"px": position.tp1_px,
                                       "pnl": round(pnl, 2),
                                       "runner": position.tp2_qty})
                    position.move_stop_to_breakeven()
                    await om.rebracket_breakeven(position)
                elif action in (TP2_FILL, STOP, FLATTEN):
                    reason = {"TP2_FILL": "target-2", "STOP": "stop",
                              "FLATTEN": "pre-halt-flat"}[action]
                    leg = {"TP2_FILL": "tp2", "STOP": "stop",
                           "FLATTEN": "exit"}[action]
                    fq = position.qty
                    px = {"TP2_FILL": position.tp2_px,
                          "STOP": position.stop_px,
                          "FLATTEN": bid if position.side == "long"
                          else ask}[action]
                    pnl = position.on_fill(fq, px, leg)
                    pnl -= om.commission_rt * fq
                    risk.register_close(pnl)
                    await emit("CLOSED", {
                        "trigger": position.trigger, "reason": reason,
                        "pnl": round(pnl, 2),
                        "mfe_pts": round(position.max_fav, 2),
                        "mae_pts": round(position.max_adv, 2),
                        "held_min": position.held_minutes(now)})
                    position = None
            else:
                # live: the exchange drives fills via native OCA brackets
                for leg, fq, px in om.check_native_fills(position):
                    pnl = position.on_fill(fq, px, leg)
                    await emit("NATIVE_FILL", {"leg": leg, "qty": fq,
                                               "px": round(px, 2),
                                               "pnl": round(pnl, 2)})
                    if leg == "tp1":
                        await om.rebracket_breakeven(position)
                    elif leg in ("stop", "tp2"):
                        pnl -= om.commission_rt * fq
                        risk.register_close(pnl)
                        await emit("CLOSED", {
                            "trigger": position.trigger,
                            "reason": "native-" + leg,
                            "pnl": round(pnl, 2),
                            "held_min": position.held_minutes(now)})
                        position = None
                        break
                if position and flatten_now:
                    pnl = await om.close(position, "pre-halt-flat", leg="exit")
                    risk.register_close(pnl)
                    await emit("CLOSED", {"trigger": position.trigger,
                                          "reason": "pre-halt-flat",
                                          "pnl": round(pnl, 2)})
                    position = None

            if position and risk.check_kill(True):
                pnl = await om.close(position, "kill-switch", leg="exit")
                risk.register_close(pnl)
                await emit("CLOSED", {"trigger": position.trigger,
                                      "reason": "kill-switch",
                                      "pnl": round(pnl, 2)})
                position = None

        if held_trigger and position is None and glx:
            glx.on_position_closed(held_trigger)

        # ---- entries ----
        glx_taken = None
        if not position and not pending_entry and spot and not flatten_now:
            ok, why = risk.can_enter(now, False)
            if ok:
                cand = None
                trigger = None
                if overnight:
                    # GEX sleeve: breakouts on frozen zones, half size,
                    # confidence bar +0.15. Then PA sleeves C/D.
                    if gex_ok:
                        cand = evaluate_breakout(now, spot, gex, wb, closes)
                        if cand is not None:
                            cand["qty"] = max(1, cand["qty"] // 2)
                            cand["risk_usd"] = true_risk_dollars(
                                cand["stop_pts"], cand["qty"])
                            cand["overnight"] = True
                            trigger = "breakout"
                    if cand is None:
                        cand = pa.evaluate_d(now, spot)
                        trigger = "pa_sweep"
                        if cand is None:
                            cand = pa.evaluate_c(now, spot)
                            trigger = "pa_fade"
                else:
                    # NY: fade + breakout off bridge levels.
                    if gex_ok:
                        cand = evaluate_breakout(now, spot, gex, wb, closes)
                        trigger = "breakout"
                        if cand is None:
                            cand = evaluate_fade(now, spot, gex, touches)
                            trigger = "fade"
                # Globex sleeve: only when no GEX/PA candidate this pass
                if cand is None and glx_sigs:
                    for gs in glx_sigs:
                        c = glx.to_candidate(gs["name"], gs["signal"], spot,
                                             gex if gex_ok else None)
                        if c:
                            cand, trigger, glx_taken = c, c["trigger"], gs["name"]
                            break
                        await emit("GLOBEX_REJECT", {
                            "strategy": gs["name"], "spot": round(spot, 2),
                            "why": "stop/target already on the wrong side of price"})
                if cand:
                    await emit("CANDIDATE", {
                        k: cand[k] for k in
                        ("trigger", "sleeve", "side", "wall", "qty",
                         "risk_usd", "spot", "regime",
                         "stop_pts", "overnight", "strategy") if k in cand})
                    pending_entry = True
                    try:
                        position = await om.enter(cand, now)
                        if position is None:
                            await emit("NO_FILL", {
                                "trigger": trigger,
                                "wall": cand.get("wall")})
                            if glx_taken and glx:
                                glx.on_not_taken(glx_taken)
                        elif trigger == "fade":
                            touches[round(cand["wall"])] = \
                                touches.get(round(cand["wall"]), 0) + 1
                    finally:
                        pending_entry = False
                elif not gex_ok and last_gex_ok:
                    await emit("GEX_DOWN", {"why": gex_why,
                                            "note": "PA sleeves only"
                                            if overnight else "no entries"})
            elif why not in ("session",):
                await emit("RISK_BLOCK", {"reason": why})

        # signals that were not taken (in a position, blocked, or another won)
        if glx:
            for gs in glx_sigs:
                if gs["name"] != glx_taken:
                    glx.on_not_taken(gs["name"])

        last_gex_ok = gex_ok

        # ---- heartbeat ----
        if (now - last_beat).total_seconds() >= config.HEARTBEAT_SEC:
            last_beat = now
            pos = (f"{position.side} x{position.qty} "
                   f"@{position.entry_px:.2f}") if position else "flat"
            print(f"{now.strftime('%H:%M:%S')} ES={spot:.2f} pos={pos} "
                  f"day_pnl={risk.daily_pnl:+.0f} trades={risk.trades_today} "
                  f"regime={gex.regime} "
                  f"cw={gex.call_wall} cc={gex.call_conf:.2f} "
                  f"pw={gex.put_wall} pc={gex.put_conf:.2f} "
                  f"lvl_age={levels.age_sec():.0f}s "
                  f"{'ON' if overnight else 'NY'}", flush=True)

        await asyncio.sleep(config.LOOP_CADENCE_SEC)

    # ---- session end: flatten first ----
    await emit("SESSION_LOOP_END", {})
    if position:
        pnl = await om.close(position, "session-end", leg="exit")
        risk.register_close(pnl)
        await emit("CLOSED", {"trigger": position.trigger,
                              "reason": "session-end",
                              "pnl": round(pnl, 2)})
    await emit("DAY_END", {"trades": risk.trades_today,
                           "day_pnl": round(risk.daily_pnl, 2)})
    try:
        om.shutdown()
    except Exception:
        pass
    try:
        mt5data.shutdown()
    except Exception:
        pass


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="simulate only; do NOT transmit "
                         "(default: LIVE)")
    ap.add_argument("--log-dir", default=config.LOG_DIR)
    ap.add_argument("--oneshot", action="store_true",
                    help="run one session then exit (debugging); "
                         "default is always-on: sleep and retry next session")
    args = ap.parse_args()
    dry_run = args.dry_run

    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _shutdown.set)
    except NotImplementedError:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: _shutdown.set())

    await emit("DAEMON", {"mode": "oneshot" if args.oneshot else "always-on",
                          "dry_run": dry_run, "executor": "mt5"})
    print(f"mode={'ONESHOT' if args.oneshot else 'ALWAYS-ON'} | "
          f"{'DRY-RUN' if dry_run else 'LIVE-MT5-DEMO'}", flush=True)

    if args.oneshot:
        if config.in_trading_session(datetime.now(ET)):
            await run_session(dry_run)
        else:
            await emit("NO_TRADE_DAY", {"reason": "off-session (oneshot)"})
            print("off-session; oneshot exits", flush=True)
        return

    # always-on supervisor: each session runs fresh; between sessions we
    # sleep until Globex reopens.
    while not _shutdown.is_set():
        now = datetime.now(ET)
        if config.in_trading_session(now):
            await run_session(dry_run)
        else:
            await emit("NO_TRADE_DAY", {"reason": "off-session-waiting"})
            await sleep_until_next_session()
        if _shutdown.is_set():
            break
    await emit("SHUTDOWN", {"reason": "signal"})


if __name__ == "__main__":
    asyncio.run(main())

"""0DTE SPXW crush-entry algo - main loop (CONSUMER mode).

The bridge (gex_bridge/) is the account's single streaming connection.
This process holds ZERO market-data lines: it connects to IBKR order-only
(clientId=7, paper-gated) and reads everything -- chain quotes, walls,
candidates, regime -- from ../shared/levels.json (atomic publish).

Fail-safe: no NEW entries when levels.json is missing or older than 60s
in a NY session (STALE_LEVELS); open positions keep being managed.

ALWAYS-ON (default): the process never exits on its own. Each session is
connect (paper-gated) -> wait for bridge levels -> trade -> 15:55 flatten
-> 16:05 disconnect; then it sleeps (interruptibly) until the next weekday
09:30 ET. Ctrl+C / SIGTERM always exits promptly (flatten first).
Pass --oneshot for a single session (debugging).

Default is DRY-RUN (no orders). Pass --live to transmit on the PAPER
account (still hard-gated to DU* paper accounts; live money impossible).
"""
import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
from levels import LevelsWatcher
from ibkr_conn import connect_ib, ensure_connected, account_net_liq
from strategy import (HOLD, T1_FILL, T2_FILL, TP_TOUCH, TRAIL_STOP, STOP,
                      FLATTEN)
from orders import OrderManager, COMMISSION
from risk import RiskManager

ET = ZoneInfo("America/New_York")
log = logging.getLogger("algo")

_shutdown = asyncio.Event()
_log_handlers: list = []


def setup_logging(log_dir: str):
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
    day = datetime.now(ET).strftime("%Y%m%d")
    path = os.path.join(log_dir, f"algo_{day}.jsonl")
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
    logging.getLogger("algo").info(json.dumps(rec))


def next_session_start(now_et: datetime) -> datetime:
    """Next weekday 09:30 ET at or after now."""
    cand = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if cand <= now_et:
        cand += timedelta(days=1)
    while cand.weekday() >= 5:
        cand += timedelta(days=1)
    return cand


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
    target = next_session_start(now)
    secs = (target - now).total_seconds()
    if secs <= 0:
        return
    await emit("SLEEP", {"until": target.isoformat(), "secs": int(secs)})
    print(f"sleeping {int(secs)}s until next session {target.strftime('%a %H:%M %Z')}",
          flush=True)
    await sleep_interruptible(secs)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="simulate only; do NOT transmit (default: LIVE)")
    ap.add_argument("--log-dir", default=config.LOG_DIR)
    ap.add_argument("--oneshot", action="store_true",
                    help="run one session (or one day-check) then exit; "
                         "default is always-on: sleep and retry next session")
    args = ap.parse_args()
    dry_run = args.dry_run

    setup_logging(args.log_dir)

    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _shutdown.set)
    except NotImplementedError:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: _shutdown.set())

    await emit("DAEMON", {"mode": "oneshot" if args.oneshot else "always-on",
                          "dry_run": dry_run})
    print(f"mode={'DRY-RUN' if dry_run else 'LIVE-PAPER'} | "
          f"{'ONESHOT' if args.oneshot else 'ALWAYS-ON'}", flush=True)

    if args.oneshot:
        await run_session(dry_run, args)
        return

    while not _shutdown.is_set():
        try:
            await run_session(dry_run, args)
        except SystemExit as e:
            try:
                await emit("SESSION_ABORT", {"error": str(e)})
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001 - stay alive on transient faults
            log.exception("session crashed; retrying shortly")
            try:
                await emit("SESSION_ERROR",
                           {"error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass
            await sleep_interruptible(300)
            continue
        if _shutdown.is_set():
            break
        await sleep_until_next_session()
    try:
        await emit("DAEMON_STOP", {"reason": "shutdown-signal"})
    except Exception:
        pass


async def _await_ny_levels(levels: LevelsWatcher, timeout: float = 600):
    """Wait for the bridge to publish a NY session payload."""
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end and not _shutdown.is_set():
        lv = levels.get()
        if lv is not None and lv.get("session") == "ny":
            return lv
        await asyncio.sleep(5)
    return None


async def run_session(dry_run: bool, args):
    """One trading session: connect (order-only) -> wait for bridge levels
    -> trade -> 15:55 flatten -> 16:05 disconnect."""
    log_path = setup_logging(args.log_dir)
    print(f"logging to {log_path} | mode={'DRY-RUN' if dry_run else 'LIVE-PAPER'}",
          flush=True)
    await emit("START", {"mode": "dry-run" if dry_run else "live-paper",
                         "port": config.IB_PORT, "client_id": config.IB_CLIENT_ID})

    # ---- connect (order-only, paper-gated) ----
    ib, account = await connect_ib()
    await emit("CONNECTED", {"account": account})

    # ---- bridge levels ----
    levels = LevelsWatcher()
    lv = await _await_ny_levels(levels)
    if lv is None:
        await emit("NO_TRADE_DAY", {"reason": "no-ny-levels (bridge down?)"})
        ib.disconnect()
        return
    await emit("LEVELS_OK", {"session": lv.get("session"),
                             "age_s": round(levels.age_sec(), 1)})

    # ---- order contracts from the bridge's descriptors (one-shot qualify) ----
    om_probe = OrderManager(ib, account, dry_run, levels.quote, emit)
    contracts = om_probe.bind_contract_descriptors(levels.load_contracts())
    if not contracts:
        await emit("NO_TRADE_DAY", {"reason": "empty contracts.json"})
        ib.disconnect()
        return
    await ib.qualifyContractsAsync(*contracts.values())
    await emit("CONTRACTS", {"n": len(contracts)})

    # ---- regime (from the bridge; observe by default) ----
    rinfo = levels.regime()
    regime_score = rinfo.get("score")
    await emit("REGIME", {"mode": rinfo.get("mode", config.REGIME_MODE),
                          "score": regime_score,
                          "detail": rinfo.get("detail")})

    # ---- risk / orders ----
    acct_val = account_net_liq(ib, account)
    risk = RiskManager(acct_val)
    await emit("RISK", {"net_liq": acct_val,
                        "daily_stop": round(risk.daily_stop_amt, 2),
                        "risk_per_trade": config.RISK_PER_TRADE})
    if rinfo.get("mode") == "live" and regime_score is not None:
        ok = regime_score >= config.REGIME_MIN_SCORE
        mult = (config.REGIME_SIZE_MULT
                if regime_score >= config.REGIME_HIGH_SCORE else 1.0)
        risk.set_regime(ok, mult)
        if not ok:
            await emit("REGIME_GATE", {"score": regime_score,
                                       "min": config.REGIME_MIN_SCORE,
                                       "note": "no new entries today"})

    om = om_probe  # get_quote = levels.quote (bid, ask) from chain_frame

    # ---- main loop ----
    position = None
    pending_entry = False
    last_beat = datetime.now(ET)
    last_scan_log = 0

    await emit("RUN", {"note": "entering main loop (consumer mode)"})
    while not _shutdown.is_set():
        now = datetime.now(ET)
        if (now.hour, now.minute) >= config.LOOP_END:
            break
        if not await ensure_connected(ib):
            await emit("FATAL", {"reason": "reconnect-exhausted"})
            break

        risk.new_day(now.date())
        lv = levels.get()
        spot = (lv or {}).get("spx")
        walls = (lv or {}).get("walls") or {}
        lvl_ok, lvl_why = levels.entries_allowed(now)

        # ---- position management (exits first) ----
        if position and spot:
            if position.tiered_effective:
                live_fills = [(t, fq, fpx)
                              for t, fq, fpx in om.check_tier_fills(position)]
            else:
                _f = om.check_tp_fill(position)
                live_fills = [(0, _f[0], _f[1])] if _f else []
            for tier, fq, fpx in live_fills:
                if tier == 0:
                    fpnl = position.on_close(fq, fpx) - COMMISSION
                    position.qty -= fq
                else:
                    fpnl = position.apply_tier_fill(tier, fq, fpx) - COMMISSION
                risk.register_partial(fpnl)
                if tier == 0:
                    await emit("CLOSED", {"key": position.key, "reason": "tp-10x",
                                          "pnl": round(fpnl, 2),
                                          "exit_px": round(fpx, 2),
                                          "peak_x": round(position.peak_multiple, 2),
                                          "held_min": position.held_minutes(now),
                                          "window": position.window,
                                          "trigger": position.trigger,
                                          "regime_score": regime_score})
                    position = None
                    break
                await emit("TIER_FILL", {"key": position.key, "tier": tier,
                                         "qty": fq, "px": round(fpx, 2),
                                         "multiple": round(fpx / position.entry_px, 2),
                                         "pnl": round(fpnl, 2),
                                         "trigger": position.trigger,
                                         "regime_score": regime_score})
                if tier == 2 and position.t2_qty == 0:
                    q = levels.quote(position.key)
                    await om.arm_trail(position,
                                       q[0] if q and q[0] > 0 else fpx)
            position.drain_tier_fills() if position else None

            if position:
                q = levels.quote(position.key)
                bid = q[0] if q and q[0] else 0.0
                action = position.update(now, bid, spot)
                for tier, fq, fpx, fpnl0 in position.drain_tier_fills():
                    fpnl = fpnl0 - COMMISSION
                    risk.register_partial(fpnl)
                    await emit("TIER_FILL", {"key": position.key, "tier": tier,
                                             "qty": fq, "px": round(fpx, 2),
                                             "multiple": round(fpx / position.entry_px, 2),
                                             "pnl": round(fpnl, 2),
                                             "trigger": position.trigger,
                                             "regime_score": regime_score})
                if position.state == "TRAIL" and not position.trail_arm_emitted:
                    position.trail_arm_emitted = True
                    await emit("TRAIL_ARM",
                               {"key": position.key,
                                "floor": round(position.trail_floor, 2),
                                "peak": round(position.trail_peak, 2),
                                "runner_qty": position.runner_qty,
                                "trigger": position.trigger})
                if action == TP_TOUCH:
                    await om.arm_trail(position, bid)
                elif action in (STOP, TRAIL_STOP, FLATTEN):
                    reason = {"STOP": "stop-loss", "TRAIL_STOP": "trail-stop",
                              "FLATTEN": "15:55-flat"}[action]
                    pnl = await om.close(position, reason)
                    risk.register_close(pnl)
                    await emit("CLOSED", {"key": position.key, "reason": reason,
                                          "pnl": round(pnl, 2),
                                          "peak_x": round(position.peak_multiple, 2),
                                          "held_min": position.held_minutes(now),
                                          "tp_touched": position.tp_touched,
                                          "window": position.window,
                                          "trigger": position.trigger,
                                          "regime_score": regime_score,
                                          "mfe": round(position.mfe, 3),
                                          "mae": round(position.mae, 3)})
                    position = None
            if position and risk.check_kill(True):
                pnl = await om.flatten(position, "kill-switch")
                risk.register_close(pnl)
                await emit("CLOSED", {"key": position.key, "reason": "kill-switch",
                                      "pnl": round(pnl, 2),
                                      "trigger": position.trigger,
                                      "regime_score": regime_score})
                position = None

        # ---- entry evaluation (bridge-screened candidates) ----
        if not position and not pending_entry and spot:
            if not lvl_ok:
                if lvl_why.startswith("stale") and \
                        now.timestamp() - last_scan_log > 300:
                    last_scan_log = now.timestamp()
                    await emit("STALE_LEVELS", {"why": lvl_why})
            else:
                cands = levels.candidates()
                cand = cands[0] if cands else None
                if cand is not None:
                    ok, why = risk.can_enter(
                        now, False, cand.get("trigger", "crush"))
                    if ok:
                        qty = risk.size_qty(cand["ask"])
                        await emit("CANDIDATE", {
                            "key": cand["key"], "ask": cand["ask"],
                            "delta": cand.get("delta"),
                            "gamma": cand.get("gamma"),
                            "score": cand.get("score"),
                            "ask_z": cand.get("ask_z"),
                            "window": cand.get("window"), "otm": cand.get("otm"),
                            "spot": cand.get("spot"), "wall": cand.get("wall"),
                            "trigger": cand.get("trigger", "crush"),
                            "wb_side": cand.get("wb_side"),
                            "flip": (lv or {}).get("flip"),
                            "regime_score": regime_score,
                            "qty": qty})
                        pending_entry = True
                        try:
                            position = await om.enter(cand, qty, now)
                            if position is None:
                                await emit("NO_FILL", {
                                    "key": cand["key"],
                                    "trigger": cand.get("trigger", "crush")})
                        finally:
                            pending_entry = False
                    elif why not in ("time-gate",):
                        await emit("RISK_BLOCK", {"reason": why,
                                                  "trigger": cand.get("trigger",
                                                                      "crush")})

        # ---- heartbeat ----
        if (now - last_beat).total_seconds() >= config.HEARTBEAT_SEC:
            last_beat = now
            pos = (f"{position.key} x{position.qty} "
                   f"@{position.entry_px:.2f} {position.state}"
                   f"[{position.trigger}]"
                   + (f" flr={position.trail_floor:.2f}"
                      if position.state == "TRAIL" else "")
                   ) if position else "flat"
            print(f"{now.strftime('%H:%M:%S')} spot={spot:.1f} pos={pos} "
                  f"day_pnl={risk.daily_pnl:+.0f} trades={risk.trades_today} "
                  f"lvl_age={levels.age_sec():.0f}s",
                  flush=True)

        await asyncio.sleep(config.LOOP_CADENCE_SEC)

    # ---- shutdown: flatten first ----
    await emit("SHUTDOWN", {"reason": "loop-end" if not _shutdown.is_set() else "signal"})
    if position:
        pnl = await om.flatten(position, "shutdown")
        risk.register_close(pnl)
        await emit("CLOSED", {"key": position.key, "reason": "shutdown",
                              "pnl": round(pnl, 2)})
    await emit("DAY_END", {"trades": risk.trades_today,
                           "day_pnl": round(risk.daily_pnl, 2)})
    ib.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

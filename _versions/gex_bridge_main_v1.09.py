#!/usr/bin/env python3
"""
v1.09 2026-10-02 [SPXFIRST + NOSNAPGENERIC] Live 09:33 the day setup aborted
    with "no SPX spot": _await_spot() polled the SPX ticker, but the SPX line
    was only subscribed later inside start_streaming(), so the wait could
    never succeed. SPX is now subscribed (chain.ensure_spx) BEFORE the OI
    scan, so spot is live by the time it is needed. Same restart also fixes
    the OI scan returning 0/602 (see chain.py v1.01).
v1.08 2026-10-02 [DASHPROFILE] after each 5-min wall evaluation the per-strike
    GEX profile is written to shared/gex_profile.json for the read-only
    dashboard (guarded; levels.json unchanged).
gex_bridge: the single IBKR streaming connection.

ALWAYS-ON supervisor loop:
  weekend (Sat, Fri>=17:00, Sun<17:55 ET) -> sleep until Sun 17:55 ET
  NY 09:30-16:05 ET:
      SPX index + ES front futures + SPXW 0DTE chain (spot+/-50, 5-pt)
      + crush band (55-95 OTM, 10-pt steps) = 64 sustained lines.
      Frozen morning OI snapshot x live gamma -> StableWall estimator
      (5-min wall clock) -> 0DTE crush + wall-break candidate screens.
      Publishes shared/levels.json every 5s (atomic tmp+rename).
  overnight:
      chain streaming cancelled (lines freed), SPX + ES futures kept.
      Walls frozen: published with stale=true, wall_ts_utc, wall_age_min,
      confidence decayed x max(0.3, 1 - age_min/240).
      Publishes every 15s.

This process NEVER places orders. Data only. Consumers:
  algo/    0DTE options algo  (IBKR order-only connection, clientId=7)
  algo_es/ ES futures algo    (MT5 execution, zero IBKR)

Deploy order on the VPS: start the bridge FIRST, then the algos.
All three folders must be siblings so ../shared resolves.
"""
import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from ib_insync import ContFuture

import config
import ibkr_conn
from chain import ChainStream
from pacer import Pacer
from publish import publish
import stable_gex
import screen
import regime as regime_mod

log = logging.getLogger("bridge")


# ---------------------------------------------------------------- helpers
def now_et() -> datetime:
    return datetime.now(config.ET)


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def setup_logging(log_dir: str):
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s",
                            datefmt="%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.join(log_dir, "bridge.log"))
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(ch)


def _es_price(t) -> float | None:
    if t is None:
        return None
    for v in (t.last, t.close, t.markPrice):
        try:
            if v and v > 0:
                return float(v)
        except (TypeError, ValueError):
            pass
    return None


def _round_q(x, tick=0.25):
    return round(round(x / tick) * tick, 2)


# ---------------------------------------------------------------- day setup
async def _ensure_day(ib, pacer, state, now) -> bool:
    """Start (or restart) the NY chain session for today's expiry."""
    day = now.strftime("%Y-%m-%d")
    if state.get("day") == day and state.get("chain_active"):
        return True
    expiry = now.strftime("%Y%m%d")
    log.info("=== NY session start: expiry %s ===", expiry)
    old = state.get("chain")
    if old is not None:
        try:
            old.stop_streaming()
        except Exception:
            pass
    chain = ChainStream(ib, pacer)
    n = await chain.discover(expiry)
    if n == 0:
        log.error("chain discovery failed for %s", expiry)
        return False
    _publish_contracts(chain, expiry)
    await chain.ensure_spx()          # [v1.09 SPXFIRST] spot must be streaming first
    oi = await chain.morning_oi_snapshot()
    if not oi:
        log.error("morning OI snapshot failed")
        return False
    spot0 = await _await_spot(chain)
    if not spot0:
        log.error("no SPX spot; aborting day setup")
        return False
    await chain.start_streaming(spot0)
    await chain.wing_sweep(spot0)
    reg_features = await regime_mod.morning_snapshot(ib, spot0)
    state.update(day=day, chain=chain, chain_active=True,
                 gex=stable_gex.GexState(), wb=screen.WallBreakState(),
                 reg_features=reg_features,
                 spot_closes=regime_mod.SpotCloses(),
                 spot0=spot0, regime_score=None, regime_detail={},
                 regime_done=False, frozen=None,
                 last_recenter=now, candidates=[],
                 oi_ts_utc=_utc_iso_now())
    await _ensure_es(ib, state)
    log.info("NY session live: spot=%.1f lines=%d (of %d)",
             spot0, len(chain.stream) + 1, config.LINE_HARD_CAP)
    return True


async def _await_spot(chain, tries: int = 40) -> float | None:
    for _ in range(tries):
        s = chain.spot()
        if s and s > 0:
            return s
        await asyncio.sleep(0.5)
    return None


async def _ensure_es(ib, state):
    if state.get("es_ticker") is not None:
        return
    es = ContFuture(config.ES_SYMBOL, exchange=config.ES_EXCHANGE)
    await ib.qualifyContractsAsync(es)
    t = ib.reqMktData(es, "", False, False)
    state["es_ticker"] = t
    log.info("ES futures subscribed: %s", es.localSymbol)


def _publish_contracts(chain, expiry):
    descs = {}
    for (strike, right), c in chain.contracts.items():
        descs[f"{strike:g}{right}"] = {
            "symbol": c.symbol, "secType": c.secType,
            "exchange": c.exchange, "currency": c.currency,
            "lastTradeDateOrContractMonth": c.lastTradeDateOrContractMonth,
            "strike": c.strike, "right": c.right,
            "multiplier": c.multiplier, "tradingClass": c.tradingClass,
        }
    publish(config.CONTRACTS_PATH,
            {"ts_utc": _utc_iso_now(), "expiry": expiry,
             "contracts": descs})
    log.info("contracts.json published: %d contracts, expiry %s",
             len(descs), expiry)


def _chain_frame(chain) -> dict:
    """Quotes + Greeks frame for consumers (pricers/quote lookups)."""
    frame = {}
    for (strike, right), t in chain.stream.items():
        bid = t.bid if t.bid and t.bid > 0 else None
        ask = t.ask if t.ask and t.ask > 0 else None
        if bid is None and ask is None:
            continue
        g = t.modelGreeks
        frame[f"{strike:g}{right}"] = {
            "bid": round(bid, 2) if bid else None,
            "ask": round(ask, 2) if ask else None,
            "delta": round(g.delta, 4) if g and g.delta else None,
            "iv": round(g.impliedVol, 4) if g and g.impliedVol else None,
            "gamma": round(g.gamma, 6) if g and g.gamma else None,
        }
    return frame


# ---------------------------------------------------------------- levels payload
def _es_walls(spx_walls: dict, basis: float) -> dict:
    out = {}
    for side in ("call", "put"):
        w = spx_walls.get(side) or {}
        if not w.get("strike"):
            out[side] = None
            continue
        out[side] = {
            "strike": _round_q(w["strike"] + basis),
            "zone_lo": _round_q(w["zone_lo"] + basis),
            "zone_hi": _round_q(w["zone_hi"] + basis),
            "confidence": w["confidence"],
            "tenure_min": w.get("tenure_min", 0.0),
            "gex_b": w.get("gex_b"),
            "dominance": w.get("dominance"),
        }
    return out


def _decay(conf: float, age_min: float) -> float:
    f = max(config.CONF_DECAY_FLOOR, 1.0 - age_min / config.CONF_DECAY_MIN)
    return round(conf * f, 3)


def _publish_profile(gex, spot):
    """[v1.08 DASHPROFILE] shared/gex_profile.json for the read-only dashboard.
    Separate from levels.json (the algos never read it) and fully guarded:
    a failure here is logged and ignored, it can never stop the bridge."""
    try:
        prof = getattr(gex, "profile", None)
        if not prof:
            return
        doc = dict(prof, ts_utc=_utc_iso_now(),
                   call_wall=gex.call_wall, call_zone=gex.call_zone,
                   call_conf=round(gex.call_conf, 3),
                   put_wall=gex.put_wall, put_zone=gex.put_zone,
                   put_conf=round(gex.put_conf, 3),
                   flip=gex.flip, regime=gex.regime, magnets=gex.magnets,
                   net_total_b=round(gex.net_total, 3))
        publish(os.path.join(config.SHARED_DIR, "gex_profile.json"), doc)
    except Exception as e:  # noqa: BLE001
        log.warning("gex_profile publish failed (ignored): %s", e)


def build_payload(state, now, sess: str) -> dict | None:
    """Assemble levels.json. Returns None if nothing publishable yet."""
    spx = state["chain"].spot() if state.get("chain") else None
    es = _es_price(state.get("es_ticker"))
    if sess == "ny":
        chain = state["chain"]
        ts = time.time()
        g = state["gex"].walls_payload(ts)
        basis = (es - spx) if (es and spx) else state.get("basis")
        if basis is None:
            return None
        state["basis"] = basis
        walls = {
            "call": {"strike": g["call_wall"],
                     "zone_lo": g["call_zone"][0] if g["call_zone"] else None,
                     "zone_hi": g["call_zone"][1] if g["call_zone"] else None,
                     "confidence": g["call_conf"],
                     "tenure_min": g["call_tenure_min"],
                     "dominance": None},
            "put": {"strike": g["put_wall"],
                    "zone_lo": g["put_zone"][0] if g["put_zone"] else None,
                    "zone_hi": g["put_zone"][1] if g["put_zone"] else None,
                    "confidence": g["put_conf"],
                    "tenure_min": g["put_tenure_min"],
                    "dominance": None},
        }
        for side, right in (("call", "C"), ("put", "P")):
            w = walls[side]["strike"]
            if w:
                walls[side]["dominance"] = screen.WallBreakState._dominance(
                    w, right, chain.oi)
        payload = {
            "ts_utc": _utc_iso_now(), "session": "ny",
            "spx": round(spx, 2) if spx else None,
            "es": round(es, 2) if es else None,
            "basis": round(basis, 2), "basis_ts_utc": _utc_iso_now(),
            "walls": walls, "es_walls": _es_walls(walls, basis),
            "flip": g["flip"], "regime": g["regime"],
            "net_gex_b": g["net_total_B"], "magnets": g["magnets"],
            "wall_ts_utc": _utc_iso_now(), "stale": False,
            "wall_age_min": 0.0,
            "candidates": state.get("candidates", []),
            "chain_frame": _chain_frame(chain),
            "oi_ts_utc": state.get("oi_ts_utc"),
            "regime_info": {"mode": config.REGIME_MODE,
                            "score": state.get("regime_score"),
                            "detail": state.get("regime_detail")},
        }
        state["last_walls"] = {k: dict(v) if v else None
                               for k, v in walls.items()}
        state["last_walls"].update(flip=g["flip"], regime=g["regime"],
                                   net_gex_b=g["net_total_B"],
                                   magnets=g["magnets"], basis=basis)
        state["wall_ts"] = time.time()
        return payload
    # ---------------- overnight: frozen walls, decayed confidence ----------
    fr = state.get("frozen")
    if not fr:
        return None
    age_min = (time.time() - state["wall_ts"]) / 60.0
    basis = state.get("basis")
    if basis is None:
        return None
    walls = {}
    for side in ("call", "put"):
        w = fr[side]
        if not w or not w.get("strike"):
            walls[side] = None
            continue
        walls[side] = dict(w, confidence=_decay(w["confidence"], age_min))
    return {
        "ts_utc": _utc_iso_now(), "session": "overnight",
        "spx": round(spx, 2) if spx else None,
        "es": round(es, 2) if es else None,
        "basis": round(basis, 2),
        "basis_ts_utc": state.get("basis_ts_utc"),
        "walls": walls, "es_walls": _es_walls(walls, basis),
        "flip": fr["flip"], "regime": fr["regime"],
        "net_gex_b": fr["net_gex_b"], "magnets": fr["magnets"],
        "wall_ts_utc": state.get("wall_ts_utc"),
        "stale": True, "wall_age_min": round(age_min, 1),
        "candidates": [],
        "chain_frame": {},
        "oi_ts_utc": state.get("oi_ts_utc"),
        "regime_info": {"mode": config.REGIME_MODE,
                        "score": state.get("regime_score"),
                        "detail": state.get("regime_detail")},
    }


# ---------------------------------------------------------------- transitions
def _freeze_walls(state):
    """Overnight transition: cancel chain lines, freeze walls."""
    chain = state.get("chain")
    if chain is not None:
        try:
            chain.cancel_options()
        except Exception as e:  # noqa: BLE001
            log.warning("cancel_options failed: %s", e)
    state["chain_active"] = False
    lw = state.get("last_walls")
    if lw:
        state["frozen"] = {"call": lw.get("call"), "put": lw.get("put"),
                           "flip": lw.get("flip"), "regime": lw.get("regime"),
                           "net_gex_b": lw.get("net_gex_b"),
                           "magnets": lw.get("magnets")}
        state["wall_ts_utc"] = _utc_iso_now()
        state["basis_ts_utc"] = _utc_iso_now()
    log.info("overnight: chain lines cancelled, walls frozen "
             "(conf decay x max(0.3, 1-age/240))")


# ---------------------------------------------------------------- main loop
async def _weekend_sleep(now):
    wake = config.next_wake(now)
    secs = max(60.0, (wake - now).total_seconds())
    log.info("weekend: sleeping until %s (%.1f h)",
             wake.strftime("%a %H:%M ET"), secs / 3600)
    await asyncio.sleep(min(secs, 6 * 3600))  # re-check every 6h


async def amain(args):
    setup_logging(config.LOG_DIR)
    log.info("gex_bridge starting (clientId=%d, data only)",
             config.IB_CLIENT_ID)
    pacer = Pacer(config.PACER_MKT_PER_SEC, config.PACER_MAX_CONCURRENT)
    ib = await ibkr_conn.connect_ib()
    state: dict = {}
    last_pub = 0.0
    hb = 0

    while True:
        try:
            now = now_et()
            sess = config.session(now)

            if sess == "weekend":
                await _weekend_sleep(now)
                continue

            was_up = ib.isConnected()
            if not await ibkr_conn.ensure_connected(ib):
                await asyncio.sleep(30)
                continue
            if not was_up:
                # [v1.05 RESILIENT] a reconnect drops every market-data
                # subscription: throw the chain away so _ensure_day/_ensure_es
                # resubscribe instead of publishing frozen quotes forever.
                log.warning("IBKR reconnected - rebuilding chain + ES subscriptions")
                state.clear()

            await _ensure_es(ib, state)

            if sess == "ny":
                state["frozen"] = None
                ok = await _ensure_day(ib, pacer, state, now)
                if not ok:
                    await asyncio.sleep(30)
                    continue
                chain, gex = state["chain"], state["gex"]
                spot = chain.spot()
                if spot and spot > 0:
                    chain.note_quotes(now)
                    state["spot_closes"].note(now, spot)
                    ts = time.time()
                    gex.note_gamma(chain.gamma_map(), ts)
                    ev = gex.maybe_evaluate(chain.oi, spot, ts)
                    if ev:
                        _publish_profile(gex, spot)
                        p = gex.walls_payload(ts)
                        log.info("WALLS cw=%s cz=%s cc=%.2f pw=%s pz=%s "
                                 "pc=%.2f flip=%s regime=%s net=%sB",
                                 p["call_wall"], p["call_zone"], p["call_conf"],
                                 p["put_wall"], p["put_zone"], p["put_conf"],
                                 p["flip"], p["regime"], p["net_total_B"])
                    if (now - state["last_recenter"]).total_seconds() \
                            >= config.RECENTER_MIN * 60:
                        state["last_recenter"] = now
                        await chain.recenter(spot)
                        await chain.wing_sweep(spot)
                    uni = chain.eval_universe()
                    cands, _rej, _notes = screen.evaluate(
                        uni, chain, gex, now, spot)
                    for kind, info in state["wb"].note(now, spot, gex,
                                                       chain.oi):
                        log.info("wallbreak %s: %s", kind, info)
                    wbc, _, _ = screen.evaluate_wallbreak(uni, spot,
                                                          state["wb"])
                    state["candidates"] = cands + ([wbc] if wbc else [])
                    if not state["regime_done"]:
                        sc = state["spot_closes"]
                        if sc.ready(now):
                            score, detail = regime_mod.finalize(
                                state["reg_features"], sc.range_pct())
                            state["regime_done"] = True
                            state["regime_score"] = score
                            state["regime_detail"] = detail
                            log.info("REGIME score=%s detail=%s",
                                     score, detail)
                if time.time() - last_pub >= config.PUBLISH_NY_SEC:
                    payload = build_payload(state, now, "ny")
                    if payload:
                        publish(config.LEVELS_PATH, payload)
                        last_pub = time.time()
            else:  # overnight
                if state.get("chain_active"):
                    _freeze_walls(state)
                if time.time() - last_pub >= config.PUBLISH_ON_SEC:
                    payload = build_payload(state, now, "overnight")
                    if payload:
                        publish(config.LEVELS_PATH, payload)
                        last_pub = time.time()

            hb += 1
            if hb % 12 == 0:
                ch = state.get("chain")
                log.info("hb sess=%s spx=%s es=%s basis=%s lines=%d",
                         sess, ch.spot() if ch else None,
                         _es_price(state.get("es_ticker")),
                         state.get("basis"),
                         len(ch.stream) if ch else 0)

            await asyncio.sleep(config.LOOP_CADENCE_SEC)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("bridge loop error: %s", e)
            await asyncio.sleep(10)


def main():
    ap = argparse.ArgumentParser(description="gex_bridge: single IBKR streamer")
    ap.parse_args()
    try:
        asyncio.run(amain(None))
    except KeyboardInterrupt:
        log.info("bridge stopped by user")


if __name__ == "__main__":
    main()

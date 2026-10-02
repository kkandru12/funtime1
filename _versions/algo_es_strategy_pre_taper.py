"""ES futures GEX strategy: FADE (+gamma) and BREAKOUT (-gamma / wall-break).

Pure decision logic; no IBKR calls here.

FADE (their wall-bounce semantics: put_wall -> long, call_wall -> short,
target = flip):
  - gamma regime '+' (net GEX > 0), StableWall confidence >= FADE_MIN_CONFIDENCE
  - spot within FADE_TOUCH_PTS of the wall (short under call wall,
    long above put wall)
  - max FADE_MAX_TOUCH_PER_WALL entries per wall per day
  - limit entry AT the wall; stop FADE_STOP_PTS beyond; TP1 = flip
    (half, or full if qty==1), then stop -> breakeven, TP2 = magnet beyond

BREAKOUT (their WBES semantics, simplified — no pre-buy, no ES leg split):
  - dominant wall (OI >= WALL_DOMINANCE x adjacent) + stable, spot within
    BREAK_ARM_PTS
  - BREAK_CONFIRM_BARS consecutive M1 closes beyond wall + BREAK_BUFFER_PTS
    -> momentum entry with the break
  - stop BREAK_STOP_PTS back inside; target = next magnet beyond wall,
    else measured move (2x stop)
"""
import logging
from collections import deque
from datetime import datetime

import config

log = logging.getLogger("algo.strategy")


def size_contracts(stop_pts: float) -> int:
    """Contracts from dollar risk. ES granularity ($50/pt) means 1 contract
    is the floor even when stop*risk exceeds RISK_PER_TRADE — the journal
    records the true dollar risk."""
    if getattr(config, "FIXED_QTY", 0) > 0:      # [v1.03 FIXEDQTY] KK: 1 ES contract
        return int(config.FIXED_QTY)
    if stop_pts <= 0:
        return 1
    return max(1, int(config.RISK_PER_TRADE // (stop_pts * config.FUT_MULT)))


def true_risk_dollars(stop_pts: float, qty: int) -> float:
    return round(stop_pts * config.FUT_MULT * qty, 2)


# ---------------- FADE ----------------
def evaluate_fade(now_et: datetime, spot: float, gex, touches: dict):
    """Return candidate dict or None.

    Gates: FADE sleeve on, published '+' regime, and the touched wall's
    StableWall confidence >= FADE_MIN_CONFIDENCE (0.5). Entry at the NEAR
    ZONE EDGE (better fill than the wall strike; the zone is the supply).
    """
    if not config.FADE_ENABLED:
        return None
    if gex.gamma_regime() != "+":
        return None
    wall, side, edge, conf = gex.wall_for_fade(spot)
    if wall is None:
        return None
    if conf < config.FADE_MIN_CONFIDENCE:
        return None
    if touches.get(round(wall), 0) >= config.FADE_MAX_TOUCH_PER_WALL:
        return None
    direction = 1 if side == "long" else -1
    flip = gex.flip
    # target must be on the favorable side of the wall with room to run
    if flip is None:
        return None
    if side == "long" and not (flip > wall + 2):
        return None
    if side == "short" and not (flip < wall - 2):
        return None
    tp2 = gex.magnet_beyond(flip, direction)
    stop_pts = config.FADE_STOP_PTS
    qty = size_contracts(stop_pts)
    return dict(trigger="fade", side=side, wall=round(wall, 2),
                entry_px=round(edge / config.FUT_TICK) * config.FUT_TICK,
                stop_pts=stop_pts, tp1_px=flip, tp2_px=tp2,
                qty=qty,
                risk_usd=true_risk_dollars(stop_pts, qty),
                spot=round(spot, 2), flip=flip,
                regime=gex.gamma_regime(), wall_conf=conf,
                zone=gex.call_zone if side == "short" else gex.put_zone)


# ---------------- BREAKOUT ----------------
class WallBreakState:
    """Dominant-wall break detector on ES M1 closes - ZONE-EDGE version.

    ARM: call/put wall dominant (OI >= WALL_DOMINANCE x adjacent), StableWall
    confidence >= BREAKOUT_MIN_CONFIDENCE (0.35), spot within BREAK_ARM_PTS
    of the NEAR ZONE EDGE on the near side (below call zone / above put
    zone). The zone at arm time is snapshotted: break detection runs against
    the armed zone even if the estimator later republishes.
    BREAK: BREAK_CONFIRM_BARS consecutive M1 closes beyond the armed zone
    edge + BREAK_BUFFER_PTS. A touch/wick INTO the zone is not a break.
    Max BREAK_MAX_PER_WALL breaks per wall per day. Disarms if spot drifts
    > 2x arm range from the zone.
    """

    def __init__(self):
        self.armed = None            # {"wall","zone","side","right","ratio"}
        self.breaks: dict[int, int] = {}

    def note(self, spot: float, gex, oi: dict, min_conf: float | None = None):
        """Arm/disarm from current published levels. Returns event list.

        min_conf overrides BREAKOUT_MIN_CONFIDENCE (e.g. overnight on
        frozen walls: +OVERNIGHT_CONF_BUMP).
        """
        thr = (config.BREAKOUT_MIN_CONFIDENCE if min_conf is None
               else min_conf)
        events = []
        if self.armed:
            zlo, zhi = self.armed["zone"]
            mid = (zlo + zhi) / 2
            if abs(spot - mid) > config.BREAK_ARM_PTS * 2 + (zhi - zlo):
                events.append(("disarm", {"wall": self.armed["wall"],
                                          "why": "drifted"}))
                self.armed = None
        if not self.armed:
            cands = []
            if (gex.call_wall is not None and gex.call_zone is not None
                    and gex.call_conf >= thr):
                cands.append((gex.call_wall, gex.call_zone, gex.call_conf,
                              "up", "C"))
            if (gex.put_wall is not None and gex.put_zone is not None
                    and gex.put_conf >= thr):
                cands.append((gex.put_wall, gex.put_zone, gex.put_conf,
                              "down", "P"))
            def _dist(c):
                _w, (zlo, zhi), _cf, side, _r = c
                return (zlo - spot) if side == "up" else (spot - zhi)
            cands.sort(key=_dist)
            for wall, zone, conf, side, right in cands:
                if self.breaks.get(round(wall), 0) >= config.BREAK_MAX_PER_WALL:
                    continue
                zlo, zhi = zone
                near = zlo if side == "up" else zhi
                gap = (near - spot) if side == "up" else (spot - near)
                if gap < 0 or gap > config.BREAK_ARM_PTS:
                    continue
                ratio = gex.dominance(wall, right, oi)
                if ratio is not None and ratio >= config.WALL_DOMINANCE:
                    self.armed = {"wall": wall, "zone": zone, "side": side,
                                  "right": right, "ratio": round(ratio, 2),
                                  "conf": conf}
                    events.append(("arm", dict(self.armed,
                                               spot=round(spot, 2))))
                    break
        return events

    def break_signal(self, closes: deque):
        """'up' | 'down' | None: BREAK_CONFIRM_BARS consecutive M1 closes
        beyond the ARMED ZONE EDGE (+ buffer)."""
        if not self.armed or len(closes) < config.BREAK_CONFIRM_BARS:
            return None
        zlo, zhi = self.armed["zone"]
        side = self.armed["side"]
        last = list(closes)[-config.BREAK_CONFIRM_BARS:]
        buf = config.BREAK_BUFFER_PTS
        if side == "up" and all(c > zhi + buf for c in last):
            return "up"
        if side == "down" and all(c < zlo - buf for c in last):
            return "down"
        return None

    def consume(self):
        w = round(self.armed["wall"])
        self.breaks[w] = self.breaks.get(w, 0) + 1
        info = dict(self.armed)
        self.armed = None
        return info


def evaluate_breakout(now_et: datetime, spot: float, gex, wb: WallBreakState,
                      closes: deque):
    """Return candidate dict or None.

    Stop = BREAK_STOP_PTS back INSIDE from the broken zone edge.
    Target = next magnet beyond the zone edge, else measured move (2x stop).
    """
    if not config.BREAKOUT_ENABLED:
        return None
    sig = wb.break_signal(closes)
    if not sig:
        return None
    # breakout sleeve is for -gamma; a confirmed wall-break event overrides
    # the regime (their WBES fired on the event, not the sign)
    info = wb.consume()
    zlo, zhi = info["zone"]
    wall = info["wall"]
    side = "long" if sig == "up" else "short"
    direction = 1 if side == "long" else -1
    edge = zhi if sig == "up" else zlo
    stop_pts = config.BREAK_STOP_PTS
    stop_px = edge - direction * stop_pts   # back inside the broken zone
    tp = gex.magnet_beyond(edge, direction)
    if tp is None:
        tp = edge + direction * stop_pts * 2   # measured move fallback
    qty = size_contracts(stop_pts)
    return dict(trigger="breakout", side=side, wall=round(wall, 2),
                zone=info["zone"],
                entry_px=round(spot / config.FUT_TICK) * config.FUT_TICK,
                stop_pts=stop_pts, stop_px=round(stop_px, 2),
                tp1_px=None, tp2_px=round(tp, 2),
                qty=qty, risk_usd=true_risk_dollars(stop_pts, qty),
                spot=round(spot, 2), wb_ratio=info["ratio"],
                wb_conf=info["conf"], regime=gex.gamma_regime())

# ---------------- futures position ----------------
HOLD, TP1_FILL, TP2_FILL, STOP, FLATTEN = "HOLD", "TP1_FILL", "TP2_FILL", "STOP", "FLATTEN"


class Position:
    """One ES futures position.

    FADE:  limit entry at the wall -> native stop beyond wall, TP1 at flip
           (half qty; full qty when qty==1), then stop -> breakeven and
           TP2 at the magnet beyond.
    BREAKOUT: marketable entry -> native stop back inside, TP at magnet /
           measured move (single target; half-split only if qty >= 2).

    Dry-run: update() simulates stop/target touches off the ES quote.
    Live: the exchange drives fills (native OCA stop/target); the loop only
    reconciles via check_native_fills() and manages the TP1->breakeven step.
    """

    def __init__(self, side: str, qty: int, entry_px: float, stop_px: float,
                 tp1_px: float | None, tp2_px: float | None,
                 entry_time: str, trigger: str, wall: float, spot_at_entry: float,
                 simulated: bool = True):
        self.side = side            # "long" | "short"
        self.qty = qty              # remaining
        self.entry_px = entry_px
        self.stop_px = stop_px
        self.tp1_px = tp1_px
        self.tp2_px = tp2_px
        self.entry_time = entry_time
        self.trigger = trigger
        self.wall = wall
        self.spot_at_entry = spot_at_entry
        self.simulated = simulated
        self.dir = 1 if side == "long" else -1
        # split: half at TP1 (needs qty>=2), rest at TP2
        self.tp1_qty = qty // 2 if (tp1_px and qty >= 2) else (qty if tp1_px else 0)
        self.tp2_qty = qty - self.tp1_qty
        if tp1_px is None:          # breakout single-target: all at TP2
            self.tp1_qty, self.tp2_qty = 0, qty
        self.state = "OPEN"
        self.realized = 0.0
        self.max_fav = 0.0          # MFE in points
        self.max_adv = 0.0          # MAE in points
        self.stop_moved_be = False

    def _pnl(self, qty: int, px: float) -> float:
        return self.dir * (px - self.entry_px) * config.FUT_MULT * qty

    def on_fill(self, qty: int, px: float, leg: str) -> float:
        """Record a fill on 'stop' | 'tp1' | 'tp2'. Returns realized $."""
        pnl = self._pnl(qty, px)
        self.realized += pnl
        self.qty -= qty
        if leg == "tp1":
            self.tp1_qty = 0
        elif leg == "tp2":
            self.tp2_qty = 0
        log.info("%s fill: %s %d @ %.2f pnl=%+.2f", leg, self.side, qty, px, pnl)
        return pnl

    def move_stop_to_breakeven(self):
        self.stop_px = self.entry_px
        self.stop_moved_be = True
        log.info("stop -> breakeven @ %.2f", self.entry_px)

    def update(self, now_et: datetime, bid: float, ask: float,
               flatten: bool = False) -> str:
        """Dry-run simulation: check stop/target touches. Returns action."""
        if flatten:
            return FLATTEN
        if self.state != "OPEN" or not bid or not ask:
            return HOLD
        # track MFE/MAE in points
        fav = self.dir * ((bid if self.dir > 0 else ask) - self.entry_px)
        self.max_fav = max(self.max_fav, fav)
        self.max_adv = max(self.max_adv, -fav)
        # stop touch (conservative: adverse quote through the stop)
        stop_hit = (ask <= self.stop_px) if self.dir > 0 else (bid >= self.stop_px)
        if stop_hit:
            return STOP
        # TP1 touch
        if self.tp1_qty > 0 and self.tp1_px:
            tp1_hit = (bid >= self.tp1_px) if self.dir > 0 else (ask <= self.tp1_px)
            if tp1_hit:
                return TP1_FILL
        # TP2 touch
        if self.tp2_qty > 0 and self.tp2_px:
            tp2_hit = (bid >= self.tp2_px) if self.dir > 0 else (ask <= self.tp2_px)
            if tp2_hit:
                return TP2_FILL
        return HOLD

    def held_minutes(self, now_et: datetime) -> int:
        try:
            h, m = self.entry_time.split(":")
            t0 = now_et.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            return max(0, int((now_et - t0).total_seconds() // 60))
        except Exception:
            return -1


# ---------------- overnight PA sleeves (C: range fade, D: sweep+reclaim) ----
# Overnight GEX walls are frozen/stale, so these sleeves trade pure price
# action off MT5-built M1 bars (mt5_data.py) — never the bridge file.
# Pure decision logic; no MT5/IBKR calls here. Fed with (t_et, o, h, l, c)
# bars in ascending order via note_bars().


def _tick(px: float) -> float:
    return round(px / config.FUT_TICK) * config.FUT_TICK


class OvernightPA:
    """Tracks the overnight (18:00 ET ->) range and pending sweep events.

    v1.07 2026-10-02 [PARESTART] Restart-safe (seen live 00:25/00:41/00:51:
    every restart re-sold the ON high at once):
      - a C fade needs price to have been AWAY from the extreme (outside the
        touch zone) since the process started / since the last fade on that
        side -- sitting at the high on startup is not a new touch
      - fades/sweeps per side per night are saved to <LOG_DIR>/pa_state.json
        and reloaded for the same overnight session, so the per-night caps
        survive a restart

    SLEEVE C: fade the ON high/low when the range is tradeable (8-30 pts).
    SLEEVE D: a sweep (>=2 pts beyond the extreme) that closes back inside
    the pre-sweep range within 15 min -> reversal. D overrides C: any sweep
    on a side blocks further C fades at that extreme for the night.
    """

    def __init__(self):
        self.sess_start = None     # ET datetime of the 18:00 session open
        self.on_high: float | None = None
        self.on_low: float | None = None
        self._last_bar_t = None
        self.fades = {"up": 0, "down": 0}       # C trades per side per night
        self.sweep_trades = {"up": 0, "down": 0}  # D trades per side per night
        self.c_blocked = {"up": False, "down": False}
        self.pend = {"up": None, "down": None}  # pending sweep awaiting reclaim
        self._d_signal = None      # ("short"|"long", pending-info) set on reclaim
        self.need_away = {"up": True, "down": True}   # [v1.07] fresh touch only

    # ---------------- [v1.07 PARESTART] persistence ----------------
    @staticmethod
    def _state_path():
        import os
        return os.path.join(config.LOG_DIR, "pa_state.json")

    def _save(self):
        import json, os
        try:
            os.makedirs(config.LOG_DIR, exist_ok=True)
            with open(self._state_path(), "w") as f:
                json.dump({"sess": self.sess_start.isoformat() if self.sess_start else None,
                           "fades": self.fades, "sweeps": self.sweep_trades}, f)
        except Exception as e:  # noqa: BLE001
            log.warning("pa_state save failed: %s", e)

    def _load(self):
        import json
        try:
            with open(self._state_path()) as f:
                d = json.load(f)
        except Exception:
            return
        if self.sess_start and d.get("sess") == self.sess_start.isoformat():
            self.fades = {k: int(d["fades"].get(k, 0)) for k in ("up", "down")}
            self.sweep_trades = {k: int(d["sweeps"].get(k, 0)) for k in ("up", "down")}
            log.info("PA counts restored for %s: fades=%s sweeps=%s",
                     self.sess_start, self.fades, self.sweep_trades)

    # ---------------- bar feed ----------------
    def reset(self, sess_start):
        self.sess_start = sess_start
        self.on_high = None
        self.on_low = None
        self.fades = {"up": 0, "down": 0}
        self.sweep_trades = {"up": 0, "down": 0}
        self.c_blocked = {"up": False, "down": False}
        self.pend = {"up": None, "down": None}
        self._d_signal = None
        self._load()            # [v1.07] same night after a restart: keep caps
        log.info("PA overnight session reset @ %s", sess_start)

    def note_bars(self, bars):
        """bars: iterable of (t_et, o, h, l, c), ascending. Idempotent:
        only bars newer than the last processed one are consumed."""
        for t, o, h, l, c in bars:
            if self._last_bar_t is not None and t <= self._last_bar_t:
                continue
            self._last_bar_t = t
            self._note_bar(t, o, h, l, c)

    def _note_bar(self, t, o, h, l, c):
        ss = config.overnight_session_start(t)
        if self.sess_start is None or ss > self.sess_start:
            self.reset(ss)
        # 1. sweep detection vs PRIOR extremes (before folding this bar in)
        if self.on_high is not None and self.on_low is not None:
            if (h >= self.on_high + config.SWEEP_MIN
                    and self.sweep_trades["up"] < config.PA_MAX_SWEEPS_PER_SIDE
                    and self.pend["up"] is None):
                self.pend["up"] = {"extreme": h, "t": t,
                                   "rng_lo": self.on_low, "rng_hi": self.on_high}
                self.c_blocked["up"] = True   # D overrides C on this side
                log.info("PA-D sweep up: bar high %.2f vs ON high %.2f",
                         h, self.on_high)
            if (l <= self.on_low - config.SWEEP_MIN
                    and self.sweep_trades["down"] < config.PA_MAX_SWEEPS_PER_SIDE
                    and self.pend["down"] is None):
                self.pend["down"] = {"extreme": l, "t": t,
                                     "rng_lo": self.on_low, "rng_hi": self.on_high}
                self.c_blocked["down"] = True
                log.info("PA-D sweep down: bar low %.2f vs ON low %.2f",
                         l, self.on_low)
        # 2. fold the bar into the ON range
        self.on_high = h if self.on_high is None else max(self.on_high, h)
        self.on_low = l if self.on_low is None else min(self.on_low, l)
        # 3. reclaim check: pending sweep closes back inside the PRE-SWEEP
        # range within SWEEP_RECLAIM_MIN -> reversal signal (same bar counts)
        for side in ("up", "down"):
            p = self.pend[side]
            if not p:
                continue
            age_min = (t - p["t"]).total_seconds() / 60.0
            if age_min > config.SWEEP_RECLAIM_MIN:
                self.pend[side] = None   # expired: no trade, may re-arm later
                log.info("PA-D sweep %s expired without reclaim", side)
                continue
            if side == "up" and c <= p["rng_hi"]:
                self.pend[side] = None
                self._d_signal = ("short", p)
                log.info("PA-D reclaim up: close %.2f back inside %.2f",
                         c, p["rng_hi"])
            elif side == "down" and c >= p["rng_lo"]:
                self.pend[side] = None
                self._d_signal = ("long", p)
                log.info("PA-D reclaim down: close %.2f back inside %.2f",
                         c, p["rng_lo"])

    # ---------------- sleeve C: ON range fade ----------------
    def evaluate_c(self, now_et, spot):
        """Short within PA_TOUCH_PTS of ON high / long within of ON low.
        Returns a candidate dict (single-target, like breakout) or None."""
        if not config.SLEEVE_C:
            return None
        if not config.is_pa_overnight(now_et):
            return None
        if self.on_high is None or self.on_low is None:
            return None
        width = self.on_high - self.on_low
        if not (config.PA_RANGE_MIN <= width <= config.PA_RANGE_MAX):
            return None
        tol = config.PA_TOUCH_PTS
        # [v1.07] a side re-arms once price is outside its touch zone
        if not (self.on_high - tol <= spot <= self.on_high + tol):
            self.need_away["up"] = False
        if not (self.on_low - tol <= spot <= self.on_low + tol):
            self.need_away["down"] = False
        if (not self.c_blocked["up"]
                and not self.need_away["up"]
                and self.fades["up"] < config.PA_MAX_FADES_PER_SIDE
                and self.on_high - tol <= spot <= self.on_high + tol):
            return self._mk_fade("short", spot, width)
        if (not self.c_blocked["down"]
                and not self.need_away["down"]
                and self.fades["down"] < config.PA_MAX_FADES_PER_SIDE
                and self.on_low - tol <= spot <= self.on_low + tol):
            return self._mk_fade("long", spot, width)
        return None

    def _mk_fade(self, side: str, spot: float, width: float):
        extreme = self.on_high if side == "short" else self.on_low
        key = "up" if side == "short" else "down"
        stop_pts = config.PA_STOP
        stop_px = _tick(extreme + stop_pts if side == "short"
                        else extreme - stop_pts)
        if side == "short":
            tp = _tick(extreme - config.PA_RETRACE * width)
        else:
            tp = _tick(extreme + config.PA_RETRACE * width)
        qty = max(1, size_contracts(stop_pts) // 2)   # overnight: halved
        self.fades[key] += 1
        self.need_away[key] = True      # [v1.07] next fade needs a fresh touch
        self._save()
        log.info("PA-C fade %s #%d: extreme=%.2f stop=%.2f tp=%.2f qty=%d",
                 side, self.fades[key], extreme, stop_px, tp, qty)
        return dict(trigger="pa_fade", sleeve="C", side=side,
                    wall=round(extreme, 2),
                    entry_px=_tick(spot),
                    stop_pts=stop_pts, stop_px=stop_px,
                    tp1_px=None, tp2_px=tp,
                    qty=qty, risk_usd=true_risk_dollars(stop_pts, qty),
                    spot=round(spot, 2), regime="pa",
                    on_high=round(self.on_high, 2),
                    on_low=round(self.on_low, 2))

    # ---------------- sleeve D: sweep + reclaim ----------------
    def evaluate_d(self, now_et, spot):
        """Reversal on a reclaimed sweep. Consumes the signal on produce."""
        if not config.SLEEVE_D:
            return None
        if not config.is_pa_overnight(now_et):
            return None
        sig = self._d_signal
        if not sig:
            return None
        side, p = sig
        self._d_signal = None
        # stale guard: if risk blocked entries for a long time after the
        # reclaim, don't take a cold signal
        age_min = (now_et - p["t"]).total_seconds() / 60.0
        if age_min > config.SWEEP_RECLAIM_MIN + 5:
            log.info("PA-D signal gone stale (%.0f min), dropping", age_min)
            return None
        key = "up" if side == "short" else "down"
        self.sweep_trades[key] += 1
        self._save()                    # [v1.07] cap survives a restart
        extreme = p["extreme"]
        stop_px = _tick(extreme + config.SWEEP_STOP if side == "short"
                        else extreme - config.SWEEP_STOP)
        # target = opposite ON extreme (current reading)
        tp = _tick(self.on_low if side == "short" else self.on_high)
        qty = max(1, size_contracts(config.SWEEP_STOP) // 2)  # overnight: halved
        log.info("PA-D reversal %s: sweep=%.2f stop=%.2f tp=%.2f qty=%d",
                 side, extreme, stop_px, tp, qty)
        return dict(trigger="pa_sweep", sleeve="D", side=side,
                    wall=round(extreme, 2),
                    entry_px=_tick(spot),
                    stop_pts=config.SWEEP_STOP, stop_px=stop_px,
                    tp1_px=None, tp2_px=tp,
                    qty=qty,
                    risk_usd=true_risk_dollars(config.SWEEP_STOP, qty),
                    spot=round(spot, 2), regime="pa",
                    sweep_extreme=round(extreme, 2),
                    on_high=round(self.on_high, 2),
                    on_low=round(self.on_low, 2))

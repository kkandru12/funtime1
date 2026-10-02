"""0DTE candidate screens for the bridge (runs where the data lives).

Ported from algo/strategy.py v3:
  - evaluate(): 10X-OTM crush screen, measured on 5 Databento days.
    Returns the TOP-N scored candidates (bridge publishes them; the
    consumer picks the first one its risk manager allows).
  - WallBreakState / evaluate_wallbreak(): dominant-wall break sleeve.
    DEVIATION from v3: zone-edge semantics (the bridge runs the StableWall
    estimator, so walls are zones, not lines). Arm on the near zone edge;
    break = 2 consecutive M1 closes beyond the zone edge. Same windows,
    same dominance math, same outright vehicle.

Pure functions: no IBKR calls here.
"""
import logging
from collections import Counter, deque
from datetime import datetime

import config

log = logging.getLogger("bridge.screen")


def in_entry_window(now_et: datetime) -> bool:
    return config.in_entry_window(now_et)


def evaluate(universe: list[dict], chain, gex, now_et: datetime, spot: float):
    """Return (candidates[best-first], rejects Counter, notes dict).

    universe: chain.eval_universe() dicts (strike, right, ask, bid, mid,
    delta, gamma, key). Entries only inside windows A/B (cutoff 15:50).
    """
    rejects = Counter()
    notes = {}
    win = config.active_window(now_et)
    if not win:
        return [], rejects, {"reason": "outside-entry-window"}
    wid, otm_min, otm_max = win
    notes["window"] = wid
    iv_ok, iv_note = chain.iv_ok()
    notes["iv"] = iv_note
    if not iv_ok:
        rejects["iv-spike"] += len(universe)
        return [], rejects, notes

    scored = []
    for c in universe:
        ask, bid = c["ask"], c["bid"]
        otm = abs(c["strike"] - spot)
        if not (otm_min <= otm <= otm_max):
            rejects["otm-band"] += 1
            continue
        if not (config.ASK_MIN <= ask <= config.ASK_MAX):
            rejects["ask-band"] += 1
            continue
        if abs(c["delta"]) >= config.DELTA_MAX:
            rejects["delta"] += 1
            continue
        mid = c["mid"]
        if bid <= 0 or (ask - bid) / mid > config.SPREAD_MAX:
            rejects["spread"] += 1
            continue
        z = chain.ask_zscore(c["key"])
        if z is None:
            rejects["no-history"] += 1
            continue
        if z > config.ASK_Z_MAX:
            rejects["not-crushed"] += 1
            continue
        if z < config.ASK_Z_MIN:
            rejects["crush-too-deep"] += 1
            continue
        gamma = c["gamma"]
        if gamma <= 0:
            rejects["gamma<=0"] += 1
            continue
        score = gamma / ask
        scored.append((score, c, z, otm))

    if not scored:
        return [], rejects, notes
    scored.sort(key=lambda x: -x[0])
    cands = []
    for score, c, z, otm in scored[:config.TOP_CANDIDATES]:
        wall = _wall_for(c["strike"], c["right"], gex)
        cands.append(dict(key=_skey(c["key"]), strike=c["strike"],
                          right=c["right"], ask=c["ask"], bid=c["bid"],
                          delta=round(c["delta"], 4),
                          gamma=round(c["gamma"], 6),
                          iv=round(c.get("iv", 0) or 0, 4),
                          score=round(score, 4), ask_z=round(z, 2),
                          window=wid, otm=round(otm, 1),
                          spot=round(spot, 1), wall=wall,
                          trigger="crush"))
    notes["candidates"] = len(scored)
    return cands, rejects, notes


def _skey(key) -> str:
    """(strike, 'C'|'P') -> '6500C' (JSON-safe)."""
    return f"{key[0]:g}{key[1]}"


def _wall_for(strike: float, right: str, gex):
    """Nearest wall beyond the strike in the position's direction."""
    cands = []
    for w in (gex.call_wall, gex.put_wall, gex.flip):
        if w:
            cands.append(w)
    cands += [m for m in gex.magnets]
    if right == "call":
        above = [w for w in cands if w > strike]
        return min(above) if above else None
    below = [w for w in cands if w < strike]
    return max(below) if below else None


# ---------------- wall-break sleeve (zone-edge version) ----------------
class WallBreakState:
    """Dominant-wall break detector on SPX M1 closes — ZONE-EDGE version.

    ARM (windows A/B only): a StableWall call/put wall whose same-side OI
    >= WALL_DOMINANCE x the adjacent spot-side strike's OI, with spot within
    WALL_ARM_PTS of the NEAR ZONE EDGE on the near side (below call zone /
    above put zone). The zone at arm time is snapshotted: break detection
    runs against the armed zone even if the estimator later republishes.
    BREAK: two consecutive 1-min closes beyond the armed zone edge.
    A touch/wick INTO the zone is not a break.
    """

    def __init__(self):
        self.armed = None            # {"wall","zone","side","right","ratio"}
        self.breaks: dict[int, int] = {}
        self._m1_min = None
        self._m1_last = 0.0
        self.closes = deque(maxlen=4)

    def note(self, now_et: datetime, spot: float, gex, oi: dict):
        """Roll M1 closes; arm/disarm. Returns [("arm"|"disarm", info)]."""
        events = []
        minute = now_et.replace(second=0, microsecond=0)
        if self._m1_min is None:
            self._m1_min = minute
        if minute != self._m1_min:
            if self._m1_last > 0:
                self.closes.append(self._m1_last)
            self._m1_min = minute
        if spot and spot > 0:
            self._m1_last = spot

        if self.armed:
            zlo, zhi = self.armed["zone"]
            mid = (zlo + zhi) / 2
            if abs(spot - mid) > config.WALL_ARM_PTS * config.WALL_DISARM_MULT + (zhi - zlo):
                events.append(("disarm", {"wall": self.armed["wall"],
                                          "why": "drifted",
                                          "spot": round(spot, 1)}))
                self.armed = None

        if not self.armed and config.active_window(now_et) is not None:
            cands = []
            if gex.call_wall is not None and gex.call_zone is not None:
                cands.append((gex.call_wall, gex.call_zone, "up", "C"))
            if gex.put_wall is not None and gex.put_zone is not None:
                cands.append((gex.put_wall, gex.put_zone, "down", "P"))
            # nearest zone first
            def _dist(c):
                _w, (zlo, zhi), side, _r = c
                return (zlo - spot) if side == "up" else (spot - zhi)
            cands.sort(key=_dist)
            for wall, zone, side, right in cands:
                if self.breaks.get(round(wall), 0) >= config.WALLBREAK_MAX_PER_WALL:
                    continue
                zlo, zhi = zone
                near = zlo if side == "up" else zhi
                gap = (near - spot) if side == "up" else (spot - near)
                # must be on the near side and within arm range of the edge
                if gap < 0 or gap > config.WALL_ARM_PTS:
                    continue
                ratio = self._dominance(wall, right, oi)
                if ratio is not None and ratio >= config.WALL_DOMINANCE:
                    self.armed = {"wall": wall, "zone": zone, "side": side,
                                  "right": right, "ratio": round(ratio, 2)}
                    events.append(("arm", dict(self.armed,
                                               spot=round(spot, 1))))
                    break
        return events

    @staticmethod
    def _dominance(wall: float, right: str, oi: dict):
        wall_oi = oi.get((wall, right), 0.0) or 0.0
        if wall_oi <= 0:
            return None
        adj = wall - 5.0 if right == "C" else wall + 5.0
        adj_oi = oi.get((adj, right), 0.0) or 0.0
        if adj_oi <= 0:
            return float("inf")
        return wall_oi / adj_oi

    def break_signal(self):
        """'up' | 'down' | None: two consecutive M1 closes beyond the ARMED
        ZONE EDGE."""
        if not self.armed or len(self.closes) < 2:
            return None
        zlo, zhi = self.armed["zone"]
        side = self.armed["side"]
        c1, c2 = self.closes[-2], self.closes[-1]
        if side == "up" and c1 > zhi and c2 > zhi:
            return "up"
        if side == "down" and c1 < zlo and c2 < zlo:
            return "down"
        return None

    def consume_break(self):
        w = round(self.armed["wall"])
        self.breaks[w] = self.breaks.get(w, 0) + 1
        info = dict(self.armed)
        self.armed = None
        return info


def evaluate_wallbreak(universe: list[dict], spot: float, wb: WallBreakState):
    """On a confirmed zone-edge break, pick the best gamma/ask outright on
    the break side within WALLBREAK_OTM_MAX beyond the broken edge.
    NO crush z-band: breaks are momentum events, not crush events."""
    sig = wb.break_signal()
    if not sig:
        return None, {}, {}
    right = "call" if sig == "up" else "put"
    info = wb.consume_break()
    zlo, zhi = info["zone"]
    edge = zhi if sig == "up" else zlo
    scored = []
    for c in universe:
        if c["right"] != right:
            continue
        k = c["strike"]
        if sig == "up" and not (edge < k <= edge + config.WALLBREAK_OTM_MAX):
            continue
        if sig == "down" and not (edge - config.WALLBREAK_OTM_MAX <= k < edge):
            continue
        ask, bid = c["ask"], c["bid"]
        if not (config.ASK_MIN <= ask <= config.ASK_MAX):
            continue
        if abs(c["delta"]) >= config.DELTA_MAX:
            continue
        mid = c["mid"]
        if bid <= 0 or (ask - bid) / mid > config.SPREAD_MAX:
            continue
        if c["gamma"] <= 0:
            continue
        scored.append((c["gamma"] / ask, c))
    if not scored:
        return None, {}, {"reason": "no-break-candidate"}
    scored.sort(key=lambda x: -x[0])
    score, c = scored[0]
    cand = dict(key=_skey(c["key"]), strike=c["strike"], right=c["right"],
                ask=c["ask"], bid=c["bid"], delta=round(c["delta"], 4),
                gamma=round(c["gamma"], 6), iv=round(c.get("iv", 0) or 0, 4),
                score=round(score, 4), ask_z=None,
                window="WB", otm=round(abs(c["strike"] - spot), 1),
                spot=round(spot, 1), wall=info["wall"],
                zone=[zlo, zhi], trigger="wallbreak",
                wb_side=info["side"], wb_ratio=info["ratio"])
    return cand, {}, {"trigger": "wallbreak", "candidates": len(scored)}

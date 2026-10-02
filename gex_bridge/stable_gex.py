"""GEX levels via the StableWall estimator (ES edition).

WHY NOT NAIVE ARGMAX (diagnosis of the old wall-bounce):
  1. argmax over instantaneous gamma flickers — two adjacent strikes with
     similar GEX swap the lead every tick (bid/ask bounce -> model gamma
     jitter), so the "wall" jumps strike-to-strike on noise.
  2. Gamma is price-dependent — as spot approaches a strike its gamma
     inflates, so raw walls "chase" price instead of marking structure.
  3. Tick-cadence recomputation turns (1)+(2) into wall jumps. Walls are
     structural; they must NOT update at tick frequency.

THE ESTIMATOR:
  - Wall clock: re-evaluate every ES_GEX_EVAL_SEC (300s). The 5s trade loop
    only READS the last published walls.
  - Inputs per evaluation: gamma_TWAP15 per strike (15-min time-weighted
    average of IBKR-sent gamma — kills tick noise) x frozen OI (hourly FOP
    sweep) -> dollar gamma: OI x gamma x spot^2 x 50 / 1e9 ($B).
  - Strike smoothing: GEX_s[k] = 0.25*GEX[k-5] + 0.5*GEX[k] + 0.25*GEX[k+5]
    on the 5-pt grid. Adjacent-strike flicker merges into one hump.
  - Hysteresis (Schmitt trigger): the incumbent wall keeps status until a
    challenger exceeds 1.25x its smoothed GEX on 3 CONSECUTIVE 5-min
    evaluations (15 min). Genuine regime shifts still pass; flicker never.
  - Zone, not a line: wall zone = contiguous strikes with
    GEX_s > 0.70 x GEX_s[wall] -> published [zone_lo, zone_hi].
  - Confidence: min(1, tenure_min/60) x min(1, margin/0.5),
    margin = (GEX[wall]-GEX[runner-up])/GEX[runner-up], runner-up = best
    same-side smoothed strike OUTSIDE the wall zone.
  - Flip (net-GEX sign): same hysteresis — the sign must hold 3 consecutive
    evaluations before the published regime flips.
  - Break detection (strategy.py) uses ZONE EDGES: 2 consecutive 1-min
    closes beyond the zone edge. A touch/wick into the zone is not a break.

Backportable to ~/workspace/algo/gex.py later (same pattern, mult=100).
"""
import logging
import time
from collections import deque

import config

log = logging.getLogger("algo.gex")

EVAL_SEC = float(getattr(config, "GEX_EVAL_SEC", 300))
TWAP_SEC = 900.0            # 15-min gamma TWAP
HYST_MULT = 1.25            # challenger must exceed incumbent by this
HYST_EVALS = 3              # ... on this many consecutive evaluations
ZONE_FRAC = 0.70            # zone = contiguous strikes above this x wall GEX
DOLLAR_SCALE = 1e9          # publish dollar-gamma in $B
GRID_STEP = 5.0


class GexState:
    def __init__(self, mult: float = None):
        self.mult = mult if mult else config.GEX_MULT
        self.oi: dict = {}
        # ---- published (5-min wall clock) ----
        self.call_wall: float | None = None
        self.put_wall: float | None = None
        self.call_zone: tuple | None = None   # (lo, hi)
        self.put_zone: tuple | None = None
        self.call_conf: float = 0.0
        self.put_conf: float = 0.0
        self.flip: float | None = None
        self.regime: str = "flat"             # '+', '-', 'flat' (hysteresis-gated)
        self.magnets: list[float] = []
        self.net_total: float = 0.0           # $B, smoothed
        self.spot: float = 0.0
        self.evals: int = 0
        # ---- internals ----
        self._samples: dict[tuple, deque] = {}   # (strike,right) -> [(ts, gamma)]
        self._last_eval: float = 0.0
        self._walls = {
            "C": {"inc": None, "since": 0.0, "chal": None, "count": 0},
            "P": {"inc": None, "since": 0.0, "chal": None, "count": 0},
        }
        self._regime_sign: int = 0
        self._regime_count: int = 0

    # ---------------- tick-path: only records ----------------
    def note_gamma(self, gamma_map: dict, ts: float = None):
        """Called every loop iteration. Appends (ts, gamma) samples; prunes
        anything older than TWAP_SEC. Never recomputes walls."""
        ts = ts if ts is not None else time.time()
        cutoff = ts - TWAP_SEC
        for key, g in gamma_map.items():
            if not g or g <= 0:
                continue
            dq = self._samples.setdefault(key, deque())
            dq.append((ts, float(g)))
            while dq and dq[0][0] < cutoff:
                dq.popleft()

    # ---------------- 5-min wall clock ----------------
    def maybe_evaluate(self, oi: dict, spot: float, ts: float = None) -> dict | None:
        """Evaluate at most every EVAL_SEC. Returns the WALLS payload dict
        when an evaluation ran, else None. The trade loop calls this every
        iteration and reads the published attributes otherwise."""
        ts = ts if ts is not None else time.time()
        if ts - self._last_eval < EVAL_SEC:
            return None
        if not spot or spot <= 0:
            return None
        return self.evaluate(oi, spot, ts)   # evaluate() resets the clock

    def evaluate(self, oi: dict, spot: float, ts: float) -> dict:
        self._last_eval = ts   # forced evals also reset the 5-min wall clock
        self.oi = oi
        self.spot = spot
        self.evals += 1

        # 1. gamma TWAP15 per (strike, right)
        twap = {k: self._twap(k, ts) for k in self._samples}
        twap = {k: v for k, v in twap.items() if v and v > 0}

        # 2. dollar gamma ($B) per strike per side
        raw: dict[str, dict[float, float]] = {"C": {}, "P": {}}
        for (k, r), o in oi.items():
            if not o or o <= 0:
                continue
            g = twap.get((k, r))
            if not g:
                continue
            raw[r][k] = o * g * spot * spot * self.mult / DOLLAR_SCALE

        # 3. smooth on the 5-pt grid
        sm = {r: self._smooth(raw[r]) for r in ("C", "P")}

        # 4-6. hysteresis -> wall, zone, confidence per side
        for r, attr in (("C", "call"), ("P", "put")):
            st = self._walls[r]
            raw_wall = max(sm[r], key=lambda s: sm[r][s]) if sm[r] else None
            wall = self._apply_hysteresis(st, raw_wall, sm[r], ts)
            setattr(self, f"{attr}_wall", wall)
            if wall is None:
                setattr(self, f"{attr}_zone", None)
                setattr(self, f"{attr}_conf", 0.0)
                continue
            zone = self._zone(wall, sm[r])
            setattr(self, f"{attr}_zone", zone)
            conf = self._confidence(st, wall, sm[r], zone, ts)
            setattr(self, f"{attr}_conf", conf)

        # 7. net / flip / magnets from smoothed series
        strikes = sorted(set(sm["C"]) | set(sm["P"]))
        net = {s: sm["C"].get(s, 0.0) - sm["P"].get(s, 0.0) for s in strikes}
        self.net_total = sum(net.values())
        cum, flip = 0.0, None
        for s in strikes:
            prev, cum = cum, cum + net[s]
            if (prev < 0 <= cum) or (prev > 0 >= cum):
                flip = s
                break
        self.flip = flip
        self.magnets = sorted(strikes, key=lambda s: abs(net[s]), reverse=True)[:5]

        # 8. regime sign with hysteresis
        sign = 1 if self.net_total > 0 else (-1 if self.net_total < 0 else 0)
        if sign == self._regime_sign:
            self._regime_count = 0
        else:
            self._regime_count += 1
            if self._regime_count >= HYST_EVALS:
                self._regime_sign = sign
                self._regime_count = 0
                log.info("REGIME flip -> %s (net %+.2f $B)",
                         {1: "+", -1: "-", 0: "flat"}[sign], self.net_total)
        self.regime = {1: "+", -1: "-", 0: "flat"}[self._regime_sign]

        payload = self.walls_payload(ts)
        log.info("WALLS eval #%d: %s", self.evals, payload)
        return payload

    # ---------------- internals ----------------
    def _twap(self, key: tuple, now: float) -> float | None:
        dq = self._samples.get(key)
        if not dq:
            return None
        cutoff = now - TWAP_SEC
        # time-weighted: each sample holds until the next sample (or now)
        num, den = 0.0, 0.0
        prev_t = max(dq[0][0], cutoff)
        for t, g in dq:
            if t < cutoff:
                continue
            num += g * (t - prev_t)
            den += (t - prev_t)
            prev_t = t
        num += dq[-1][1] * (now - prev_t)
        den += (now - prev_t)
        if den <= 0:
            return dq[-1][1]  # single sample exactly at eval time: use it
        return num / den

    def _smooth(self, gex: dict[float, float]) -> dict[float, float]:
        """0.25/0.5/0.25 smoothing on the uniform 5-pt grid. Missing strikes
        contribute 0 (edges are far-OTM; documented, not hidden)."""
        if not gex:
            return {}
        lo = min(gex)
        hi = max(gex)
        n = int(round((hi - lo) / GRID_STEP)) + 1
        grid = [round(lo + i * GRID_STEP, 6) for i in range(n)]
        get = {round(k, 6): v for k, v in gex.items()}
        sm = {}
        for i, s in enumerate(grid):
            c = get.get(s, 0.0)
            p = get.get(round(s - GRID_STEP, 6), 0.0)
            q = get.get(round(s + GRID_STEP, 6), 0.0)
            sm[s] = 0.25 * p + 0.5 * c + 0.25 * q
        return sm

    def _apply_hysteresis(self, st: dict, raw_wall, gex_s: dict, ts: float):
        inc = st["inc"]
        if raw_wall is None:
            return inc  # no data: hold the incumbent (fail static, not jumping)
        if inc is None:
            st["inc"], st["since"] = raw_wall, ts
            st["chal"], st["count"] = None, 0
            return raw_wall
        if raw_wall == inc:
            st["chal"], st["count"] = None, 0
            return inc
        if st["chal"] != raw_wall:
            st["chal"], st["count"] = raw_wall, 0
        g_inc = gex_s.get(inc, 0.0)
        g_chal = gex_s.get(raw_wall, 0.0)
        strong = (g_chal > HYST_MULT * g_inc) if g_inc > 0 else (g_chal > 0)
        if strong:
            st["count"] += 1
            if st["count"] >= HYST_EVALS:
                log.info("WALL switch %s -> %s (%.2f vs %.2f $B, %d evals)",
                         inc, raw_wall, g_chal, g_inc, st["count"])
                st["inc"], st["since"] = raw_wall, ts
                st["chal"], st["count"] = None, 0
                return raw_wall
        else:
            st["count"] = 0  # challenger not decisively stronger: hold
        return inc

    def _zone(self, wall: float, gex_s: dict) -> tuple:
        """Contiguous strikes with GEX_s > ZONE_FRAC x GEX_s[wall]."""
        thresh = ZONE_FRAC * gex_s[wall]
        lo = hi = wall
        while gex_s.get(round(lo - GRID_STEP, 6), 0.0) > thresh:
            lo = round(lo - GRID_STEP, 6)
        while gex_s.get(round(hi + GRID_STEP, 6), 0.0) > thresh:
            hi = round(hi + GRID_STEP, 6)
        return (lo, hi)

    def _confidence(self, st: dict, wall: float, gex_s: dict,
                    zone: tuple, ts: float) -> float:
        tenure_min = (ts - st["since"]) / 60.0
        tenure_f = min(1.0, tenure_min / 60.0)
        zlo, zhi = zone
        others = [v for s, v in gex_s.items()
                  if s != wall and not (zlo <= s <= zhi)]
        runner = max(others) if others else 0.0
        gw = gex_s[wall]
        margin = (gw - runner) / runner if runner > 0 else float("inf")
        margin_f = min(1.0, margin / 0.5)
        return round(tenure_f * margin_f, 3)

    # ---------------- published readers ----------------
    def walls_payload(self, ts: float) -> dict:
        def tenure(side):
            st = self._walls[side]
            return round((ts - st["since"]) / 60.0, 1) if st["inc"] else 0.0
        return {
            "call_wall": self.call_wall, "call_zone": self.call_zone,
            "call_conf": self.call_conf, "call_tenure_min": tenure("C"),
            "put_wall": self.put_wall, "put_zone": self.put_zone,
            "put_conf": self.put_conf, "put_tenure_min": tenure("P"),
            "flip": self.flip, "regime": self.regime,
            "net_total_B": round(self.net_total, 3),
            "magnets": self.magnets, "evals": self.evals,
        }

    def gamma_regime(self) -> str:
        """Published regime: '+', '-', 'flat' (hysteresis-gated)."""
        return self.regime

    def wall_for_fade(self, spot: float):
        """(wall, side, entry_edge, confidence) if spot touches a wall zone
        from the outside within FADE_TOUCH_PTS. Entry edge = near zone edge
        (better fill than the wall strike itself)."""
        if self.call_wall is not None and self.call_zone is not None:
            zlo, _zhi = self.call_zone
            if 0 <= zlo - spot <= config.FADE_TOUCH_PTS:
                return self.call_wall, "short", zlo, self.call_conf
        if self.put_wall is not None and self.put_zone is not None:
            _zlo, zhi = self.put_zone
            if 0 <= spot - zhi <= config.FADE_TOUCH_PTS:
                return self.put_wall, "long", zhi, self.put_conf
        return None, None, None, 0.0

    def magnet_beyond(self, ref: float, direction: int) -> float | None:
        cands = [m for m in self.magnets
                 if (direction > 0 and m > ref) or (direction < 0 and m < ref)]
        if not cands:
            return None
        return min(cands) if direction > 0 else max(cands)

    @staticmethod
    def dominance(wall: float, right: str, oi: dict, step: float = 5.0):
        """Wall OI / adjacent spot-side strike OI (same right)."""
        wall_oi = oi.get((wall, right), 0.0) or 0.0
        if wall_oi <= 0:
            return None
        adj = wall - step if right == "C" else wall + step
        adj_oi = oi.get((adj, right), 0.0) or 0.0
        if adj_oi <= 0:
            return float("inf")
        return wall_oi / adj_oi

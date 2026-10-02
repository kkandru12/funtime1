"""GEX levels from FROZEN morning OI x LIVE gamma (IBKR-sent Greeks).

GEX_strike = +/- OI x gamma x 100 x spot^2   (calls +, puts -)
Incremental: a strike's GEX is recomputed only when its gamma moved
materially (>GAMMA_CHANGE_PCT); levels re-derive on any change, plus a full
refresh every GEX_RECOMPUTE_SEC.
"""
import logging
import time

import config

log = logging.getLogger("algo.gex")


class GexState:
    def __init__(self):
        self.net: dict[float, float] = {}       # strike -> net GEX ($)
        self.magnets: list[float] = []          # top-5 |net| strikes
        self.flip: float | None = None
        self.call_wall: float | None = None
        self.put_wall: float | None = None
        self._last_gamma: dict[tuple[float, str], float] = {}
        self._last_full = 0.0
        self.spot = 0.0

    def update(self, oi: dict, gamma_map: dict, spot: float, force=False) -> bool:
        """Returns True if levels changed."""
        if not spot or spot <= 0:
            return False
        self.spot = spot
        now = time.monotonic()
        changed = force or (now - self._last_full > config.GEX_RECOMPUTE_SEC)
        if force:
            self._last_gamma = {}
        for key, g in gamma_map.items():
            if g <= 0:
                continue
            old = self._last_gamma.get(key)
            if old is None or abs(g - old) / max(old, 1e-12) > config.GAMMA_CHANGE_PCT:
                self._last_gamma[key] = g
                changed = True
        if not changed:
            return False
        self._recompute(oi, spot)
        self._last_full = now
        return True

    def _recompute(self, oi, spot):
        per: dict[float, list] = {}  # strike -> [call_gex, put_gex]
        for (k, r), o in oi.items():
            if not o:
                continue
            g = self._last_gamma.get((k, r))
            if not g or g <= 0:
                continue
            v = o * g * 100.0 * spot * spot
            d = per.setdefault(k, [0.0, 0.0])
            d[0 if r == "C" else 1] += v
        strikes = sorted(per)
        self.net = {s: per[s][0] - per[s][1] for s in strikes}
        cum, flip = 0.0, None
        for s in strikes:
            prev, cum = cum, cum + self.net[s]
            if (prev < 0 <= cum) or (prev > 0 >= cum):
                flip = s
                break
        self.flip = flip
        self.magnets = sorted(strikes, key=lambda s: abs(self.net[s]), reverse=True)[:5]
        self.call_wall = max(strikes, key=lambda s: per[s][0]) if strikes else None
        # put wall = strike with largest put-GEX magnitude (most negative put leg)
        self.put_wall = max(strikes, key=lambda s: per[s][1]) if strikes else None
        log.info("GEX levels: flip=%s magnets=%s call_wall=%s put_wall=%s",
                 flip, [f"{m:.0f}" for m in self.magnets],
                 self.call_wall, self.put_wall)

    def wall_for(self, strike: float, right: str) -> float | None:
        """Nearest wall beyond the strike in the position's direction."""
        cands = list(self.magnets)
        if self.flip:
            cands.append(self.flip)
        if self.call_wall:
            cands.append(self.call_wall)
        if self.put_wall:
            cands.append(self.put_wall)
        if right == "call":
            above = [w for w in cands if w and w > strike]
            return min(above) if above else None
        below = [w for w in cands if w and w < strike]
        return max(below) if below else None

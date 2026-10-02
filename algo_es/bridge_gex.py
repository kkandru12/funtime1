"""BridgeGex: read-only adapter over shared/levels.json.

Exposes the same interface the ES sleeves were written against (the old
local GexState): call_wall/put_wall + zones + confidence, gamma_regime(),
wall_for_fade(), magnet_beyond(), dominance(). All values in ES points —
walls come from the bridge's ES-converted es_walls (SPX wall + live basis),
magnets are converted with the same basis.

Refresh from the LevelsWatcher each loop; cheap attribute reads after.
"""
import logging

import config

log = logging.getLogger("algo_es.bridge_gex")


class BridgeGex:
    def __init__(self, levels):
        self.levels = levels
        self.call_wall = None
        self.call_zone = None
        self.call_conf = 0.0
        self.put_wall = None
        self.put_zone = None
        self.put_conf = 0.0
        self.flip = None
        self.magnets: list[float] = []
        self.regime = "flat"
        self.basis = None
        self.wall_age_min = 0.0
        self.stale = False

    def refresh(self) -> bool:
        """Pull the latest levels; populate ES-denominated attributes."""
        lv = self.levels.get()
        if not lv:
            return False
        ew = lv.get("es_walls") or {}
        basis = lv.get("basis")
        self.basis = basis
        self.stale = bool(lv.get("stale"))
        self.wall_age_min = lv.get("wall_age_min") or 0.0
        self.regime = lv.get("regime") or "flat"
        self.flip = (lv.get("flip") + basis) if lv.get("flip") and basis else None
        self.magnets = sorted(
            (m + basis) for m in (lv.get("magnets") or []) if basis) \
            if basis else []

        def _side(d: dict | None):
            if not d or not d.get("strike"):
                return None, None, 0.0
            return (d["strike"],
                    (d["zone_lo"], d["zone_hi"])
                    if d.get("zone_lo") and d.get("zone_hi") else None,
                    d.get("confidence") or 0.0)

        self.call_wall, self.call_zone, self.call_conf = _side(ew.get("call"))
        self.put_wall, self.put_zone, self.put_conf = _side(ew.get("put"))
        return True

    # ---------------- sleeve interface (mirrors the old GexState) ----------------
    def gamma_regime(self) -> str:
        return self.regime

    def wall_for_fade(self, spot: float):
        """(wall, side, entry_edge, confidence) if spot touches a wall zone
        from the outside within FADE_TOUCH_PTS. Entry edge = near zone edge."""
        if self.call_wall is not None and self.call_zone is not None:
            zlo, _zhi = self.call_zone
            if 0 <= zlo - spot <= config.FADE_TOUCH_PTS:
                return self.call_wall, "short", zlo, self.call_conf
        if self.put_wall is not None and self.put_zone is not None:
            _zlo, zhi = self.put_zone
            if 0 <= spot - zhi <= config.FADE_TOUCH_PTS:
                return self.put_wall, "long", zhi, self.put_conf
        return None, None, None, 0.0

    def magnet_beyond(self, ref: float, direction: int):
        cands = [m for m in self.magnets
                 if (direction > 0 and m > ref) or (direction < 0 and m < ref)]
        if not cands:
            return None
        return min(cands) if direction > 0 else max(cands)

    def dominance(self, wall: float, right: str, oi: dict):
        """Per-wall dominance ratios are computed by the bridge (it holds
        the OI). Match the ES wall back to its published side."""
        lv = self.levels.get() or {}
        walls = lv.get("es_walls") or {}
        side = "call" if right == "C" else "put"
        w = walls.get(side) or {}
        if w.get("strike") and abs(w["strike"] - wall) <= 0.5:
            return w.get("dominance")
        return None

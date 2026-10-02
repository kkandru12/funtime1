"""LevelsWatcher: read-only consumer of the bridge's shared/levels.json.

The bridge (gex_bridge/) is the account's single streaming connection; this
algo holds ZERO IBKR market-data lines and makes ZERO IBKR calls. Walls,
zones, confidence, regime, flip, magnets and basis arrive via the
atomically-published levels.json.

GEX sleeves (fade/breakout) stand down when levels are missing or older
than LEVELS_STALE_SEC. PA sleeves (C/D) are MT5-only and continue on the
local clock (is_pa_overnight); the bridge session flag is a cross-check.
"""
import json
import logging
import os
import time

import config

log = logging.getLogger("algo_es.levels")


def _shared_dir() -> str:
    env = os.getenv("SHARED_DIR") or os.getenv("ES_SHARED_DIR")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "shared")


class LevelsWatcher:
    def __init__(self, shared_dir: str | None = None):
        self.dir = shared_dir or _shared_dir()
        self.levels_path = os.path.join(self.dir, "levels.json")
        self._levels: dict | None = None
        self._levels_mtime = 0.0
        self._stale_warned = False

    def refresh(self) -> dict | None:
        """Re-read levels.json if it changed. Returns the payload or None."""
        try:
            mtime = os.path.getmtime(self.levels_path)
        except OSError:
            return None
        if mtime == self._levels_mtime and self._levels is not None:
            return self._levels
        try:
            with open(self.levels_path) as f:
                self._levels = json.load(f)
            self._levels_mtime = mtime
            self._stale_warned = False
        except (OSError, ValueError) as e:
            log.warning("levels.json read failed: %s", e)
            return None
        return self._levels

    def get(self) -> dict | None:
        return self.refresh()

    def age_sec(self) -> float:
        if self._levels is None:
            return float("inf")
        return time.time() - self._levels_mtime

    def gex_ok(self) -> tuple[bool, str]:
        """GEX-sleeve gate: walls must be present and reasonably fresh.

        Overnight the bridge publishes frozen walls every 15s (stale=true
        with confidence decay) — those are USABLE for breakouts at half
        size; only a truly dead bridge stands the sleeves down.
        """
        lv = self.get()
        if lv is None:
            return False, "no levels.json (bridge down?)"
        age = self.age_sec()
        if age > config.LEVELS_STALE_SEC:
            if not self._stale_warned:
                log.error("STALE_LEVELS: levels.json age %.0fs > %.0fs - "
                          "GEX sleeves stand down, PA sleeves continue",
                          age, config.LEVELS_STALE_SEC)
                self._stale_warned = True
            return False, f"stale levels ({age:.0f}s)"
        return True, "ok"

    def basis(self) -> float | None:
        lv = self._levels or {}
        return lv.get("basis")

    def session(self) -> str | None:
        lv = self._levels or {}
        return lv.get("session")

"""LevelsWatcher: read-only consumer of the bridge's shared/levels.json.

The bridge (gex_bridge/) is the account's single streaming connection; this
algo holds ZERO market-data lines. All chain quotes, walls, candidates and
regime state arrive via the atomically-published levels.json.

Fail-safe: entries are blocked when levels are missing or older than
STALE_LEVELS_SEC (60s) in a NY session. Open positions keep being managed
(exits are MT5/IBKR-native or quote-driven from the last good frame).
"""
import json
import logging
import os
import time

log = logging.getLogger("algo.levels")

STALE_LEVELS_SEC = 60.0


def _shared_dir() -> str:
    env = os.getenv("SHARED_DIR") or os.getenv("CRUSH_SHARED_DIR")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "shared")


class LevelsWatcher:
    def __init__(self, shared_dir: str | None = None):
        self.dir = shared_dir or _shared_dir()
        self.levels_path = os.path.join(self.dir, "levels.json")
        self.contracts_path = os.path.join(self.dir, "contracts.json")
        self._levels: dict | None = None
        self._levels_mtime = 0.0
        self._contracts: dict | None = None
        self._stale_warned = False

    # ---------------- levels ----------------
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
        try:
            return time.time() - self._levels_mtime
        except OSError:
            return float("inf")

    def fresh(self, max_age: float = STALE_LEVELS_SEC) -> bool:
        lv = self.get()
        if lv is None:
            return False
        return self.age_sec() <= max_age

    def entries_allowed(self, now_et) -> tuple[bool, str]:
        """Fail-safe gate: no NEW entries on stale/missing levels in NY."""
        lv = self.get()
        if lv is None:
            return False, "no levels.json (bridge down?)"
        if lv.get("session") != "ny":
            return False, f"bridge session={lv.get('session')}"
        age = self.age_sec()
        if age > STALE_LEVELS_SEC:
            if not self._stale_warned:
                log.error("STALE_LEVELS: levels.json age %.0fs > %.0fs - "
                          "entries blocked, managing open positions only",
                          age, STALE_LEVELS_SEC)
                self._stale_warned = True
            return False, f"stale levels ({age:.0f}s)"
        return True, "ok"

    # ---------------- contracts ----------------
    def load_contracts(self) -> dict:
        """contracts.json -> {key_str: descriptor}. Cached after first load."""
        if self._contracts is not None:
            return self._contracts
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                with open(self.contracts_path) as f:
                    doc = json.load(f)
                self._contracts = doc.get("contracts", {})
                log.info("contracts.json loaded: %d contracts, expiry %s",
                         len(self._contracts), doc.get("expiry"))
                return self._contracts
            except (OSError, ValueError):
                time.sleep(5)
        raise SystemExit("levels: contracts.json not available after 300s "
                         "(start gex_bridge first)")

    # ---------------- convenience readers ----------------
    def quote(self, key_str: str) -> tuple[float | None, float | None]:
        """(bid, ask) from the latest chain_frame. Key like '6560C'."""
        lv = self._levels or {}
        q = (lv.get("chain_frame") or {}).get(key_str) or {}
        return q.get("bid"), q.get("ask")

    def candidates(self) -> list[dict]:
        lv = self._levels or {}
        return lv.get("candidates") or []

    def regime(self) -> dict:
        lv = self._levels or {}
        return lv.get("regime_info") or {}

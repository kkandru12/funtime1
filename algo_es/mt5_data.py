"""MT5 market-data feed for the ES algo's overnight PA sleeves.

The overnight GEX walls are frozen/stale, so sleeves C (ON range fade) and D
(sweep+reclaim) trade pure price action. Their OHLC comes from HERE — MT5's
own M1 bars via copy_rates_from_pos (equivalent to aggregating MT5's tick
stream, without storing millions of ticks) — never from the bridge file.
The bridge file is only ever read for the session flag and GEX walls.

Fail-safe: if the MT5 package/terminal is unavailable, connect() raises and
main.py leaves the PA sleeves dormant (no bars -> no signals). Everything
else keeps running.

Bar timestamps: MT5 returns seconds-since-epoch; the zone they are in is
broker-dependent. ES_MT5_BAR_TZ (default "UTC") names the ZoneInfo they
should be interpreted in, then converted to ET. The sync log line prints
the first/last bar in ET — if the 18:00 ON-range reset looks wrong against
the wall clock, set ES_MT5_BAR_TZ to the terminal's zone.
"""
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config

log = logging.getLogger("algo.mt5data")
ET = config.ET


def _req(name: str) -> str:
    v = __import__("os").getenv(name)
    if not v:
        raise RuntimeError(
            f"MT5DataFeed: missing required env var {name} "
            f"(MT5_PATH / MT5_SERVER / MT5_LOGIN / MT5_PASSWORD / MT5_SYMBOL).")
    return v


class MT5DataFeed:
    """M1 bars + live quote for ES from the local AMP MT5 terminal."""

    def __init__(self, emit):
        self.emit = emit
        self.mt5 = None
        self.symbol = None
        self.ok = False
        self.bars: deque = deque()   # (t_et, o, h, l, c), ascending
        self._bar_tz = ZoneInfo(config.MT5_BAR_TZ)

    async def connect(self):
        """initialize() + login() + symbol resolve. Raises on any failure;
        the caller treats that as 'PA sleeves dormant'."""
        path = _req("MT5_PATH")
        server = _req("MT5_SERVER")
        login = _req("MT5_LOGIN")
        password = _req("MT5_PASSWORD")   # never logged
        try:
            import MetaTrader5 as mt5
        except ImportError:
            raise RuntimeError(
                "MT5DataFeed: the MetaTrader5 package is not installed "
                "(Windows-only; .\\venv\\Scripts\\pip install MetaTrader5).")
        self.mt5 = mt5
        ok = await asyncio.to_thread(mt5.initialize, path=path)
        if not ok:
            raise RuntimeError(
                f"MT5DataFeed: initialize() failed for MT5_PATH={path}: "
                f"{mt5.last_error()}")
        symbol = _req("MT5_SYMBOL")
        from mt5_symbol import resolve_tradeable_symbol
        resolved = await resolve_tradeable_symbol(mt5, symbol)
        info = await asyncio.to_thread(mt5.symbol_info, resolved)
        if info is None:
            raise RuntimeError(
                f"MT5DataFeed: symbol '{resolved}' not found on this terminal.")
        if not info.visible:
            await asyncio.to_thread(mt5.symbol_select, resolved, True)
        self.symbol = resolved
        try:
            login_int = int(login)
        except ValueError:
            raise RuntimeError("MT5_LOGIN must be numeric (value hidden).")
        authed = await asyncio.to_thread(
            mt5.login, login_int, password=password, server=server)
        if not authed:
            raise RuntimeError(
                f"MT5DataFeed: login failed for login={login} "
                f"server={server}: {mt5.last_error()}")
        self.ok = True
        await self.emit("MT5_DATA_CONNECTED",
                        {"symbol": resolved, "requested": symbol,
                         "bar_tz": config.MT5_BAR_TZ})
        log.info("MT5 data feed connected: symbol=%s (requested %s) bar_tz=%s",
                 resolved, symbol, config.MT5_BAR_TZ)

    def shutdown(self):
        try:
            if self.mt5:
                self.mt5.shutdown()
        except Exception:
            pass
        self.ok = False

    async def sync(self, n: int = None):
        """Pull the latest M1 bars and merge anything new. Call every loop."""
        if not self.ok:
            return
        n = n or config.MT5_BAR_LOOKBACK
        rates = await asyncio.to_thread(
            self.mt5.copy_rates_from_pos,
            self.symbol, self.mt5.TIMEFRAME_M1, 0, n)
        if not rates:
            log.warning("MT5 copy_rates_from_pos returned nothing")
            return
        added = 0
        last_t = self.bars[-1][0] if self.bars else None
        for r in rates:
            t_et = datetime.fromtimestamp(
                float(r["time"]), tz=self._bar_tz).astimezone(ET)
            if last_t is not None and t_et <= last_t:
                continue
            self.bars.append((t_et, float(r["open"]), float(r["high"]),
                              float(r["low"]), float(r["close"])))
            last_t = t_et
            added += 1
        # keep a bounded window (lookback covers a full overnight session)
        while len(self.bars) > config.MT5_BAR_LOOKBACK:
            self.bars.popleft()
        if added:
            log.debug("MT5 bars: +%d, last=%s", added,
                      self.bars[-1][0].strftime("%H:%M %Z"))

    def bars_since(self, start_et: datetime):
        """All M1 bars with bar time >= start_et (ascending)."""
        return [b for b in self.bars if b[0] >= start_et]

    def quote(self):
        """(bid, ask) | None — live MT5 quote for PA entry pricing."""
        if not self.ok:
            return None
        try:
            t = self.mt5.symbol_info_tick(self.symbol)
        except Exception:
            return None
        if t is None or not t.bid or not t.ask:
            return None
        return (float(t.bid), float(t.ask))

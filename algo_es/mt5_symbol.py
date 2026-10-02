"""Resolve an MT5 symbol to the tradable contract.

MT5_SYMBOL is often a continuous/indicative symbol (e.g. "@EP") that cannot
be traded directly. This module resolves it to the front-month futures
contract once per session (both MT5DataFeed and MT5Executor call it in
connect(), so quarterly rolls are picked up automatically).
"""
import asyncio
import logging
from datetime import datetime, timezone

log = logging.getLogger("algo_es.mt5_symbol")


def _exp_key(s):
    e = s.expiration_time
    if isinstance(e, datetime):
        if e.tzinfo is None:
            # server-time naive; a few hours of skew cannot misorder
            # quarterly expiries, so treat as UTC for ordering.
            e = e.replace(tzinfo=timezone.utc)
        return e
    return datetime.max.replace(tzinfo=timezone.utc)


async def resolve_tradeable_symbol(mt5, symbol: str) -> str:
    """Return the tradable contract for `symbol`.

    If `symbol` itself is fully tradeable, it is returned unchanged.
    Otherwise the front-month ES futures contract is picked: trade_mode FULL
    with the nearest future expiration among *EP* / *ES* symbols.
    Raises RuntimeError with candidates if nothing qualifies.
    """
    info = await asyncio.to_thread(mt5.symbol_info, symbol)
    if info is not None and info.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL:
        return symbol

    seen = {}
    for pat in ("*EP*", "*ES*"):
        try:
            for s in await asyncio.to_thread(mt5.symbols_get, pat) or ():
                seen[s.name] = s
        except Exception:
            pass
    now = datetime.now(timezone.utc)
    cands = [s for s in seen.values()
             if s.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL
             and _exp_key(s) > now]
    cands.sort(key=_exp_key)
    if not cands:
        names = sorted(seen)[:20]
        raise RuntimeError(
            f"MT5 symbol '{symbol}' is not directly tradeable and no "
            f"front futures contract found. Candidates: {names or '(none)'}")
    front = cands[0]
    log.info("resolved MT5 symbol %s -> front contract %s (exp %s)",
             symbol, front.name, front.expiration_time)
    return front.name

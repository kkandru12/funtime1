"""IBKR connection for gex_bridge: DATA ONLY.

This process NEVER places orders (no placeOrder call exists anywhere in
gex_bridge/), so there is no paper-gate here — the gate lives in the
consumer algos that actually transmit. This is the account's single
streaming connection (clientId=1); the consumers connect order-only
(algo, clientId=7) or not at all (algo_es via MT5).
"""
import asyncio
import logging

from ib_insync import IB

import config

log = logging.getLogger("bridge.ibkr")


async def connect_ib() -> IB:
    ib = IB()
    delay = 2.0
    last_err = None
    for attempt in range(1, config.CONNECT_RETRIES + 1):
        try:
            log.info("bridge connect attempt %d -> %s:%d clientId=%d",
                     attempt, config.IB_HOST, config.IB_PORT,
                     config.IB_CLIENT_ID)
            await ib.connectAsync(config.IB_HOST, config.IB_PORT,
                                  clientId=config.IB_CLIENT_ID,
                                  timeout=config.CONNECT_TIMEOUT)
            if ib.isConnected():
                break
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("connect attempt %d failed: %s (retry in %.0fs)",
                        attempt, e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
    else:
        raise SystemExit(f"bridge: could not connect to IBKR after "
                         f"{config.CONNECT_RETRIES} tries: {last_err}")

    def _on_disconnected():
        log.error("BRIDGE DISCONNECTED from IBKR")

    ib.disconnectedEvent += _on_disconnected
    try:
        log.info("bridge connected; accounts=%s", ib.managedAccounts())
    except Exception:
        pass
    return ib


async def ensure_connected(ib: IB) -> bool:
    if ib.isConnected():
        return True
    log.warning("bridge connection lost - reconnecting")
    delay = 2.0
    for _ in range(config.CONNECT_RETRIES):
        try:
            await ib.connectAsync(config.IB_HOST, config.IB_PORT,
                                  clientId=config.IB_CLIENT_ID,
                                  timeout=config.CONNECT_TIMEOUT)
            if ib.isConnected():
                log.info("bridge reconnected OK")
                return True
        except Exception as e:  # noqa: BLE001
            log.warning("reconnect failed: %s (retry in %.0fs)", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
    log.error("bridge reconnect exhausted")
    return False

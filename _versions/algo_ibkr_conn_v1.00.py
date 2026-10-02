"""IBKR connection: exponential-backoff connect, PAPER-ONLY enforcement,
auto-reconnect on drops.

Paper check: IBKR paper-trading accounts are issued account numbers starting
with "DU". If ANY managed account is not a DU account, we refuse to run.
There is deliberately no override flag.
"""
import asyncio
import logging

from ib_insync import IB

import config

log = logging.getLogger("algo.ibkr")


def assert_paper_only(ib: IB) -> str:
    """Return the paper account id, or raise SystemExit on anything else."""
    accounts = ib.managedAccounts()
    log.info("managedAccounts=%s", accounts)
    if not accounts:
        raise SystemExit("PAPER-GATE: no managed accounts reported - refusing to run")
    non_paper = [a for a in accounts if not a.startswith("DU")]
    if non_paper:
        raise SystemExit(
            f"PAPER-GATE: refusing to run - non-paper account(s) detected: {non_paper}. "
            f"This algo is PAPER ONLY."
        )
    acct = accounts[0]
    log.info("PAPER-GATE OK: paper account %s", acct)
    return acct


async def connect_ib() -> tuple[IB, str]:
    """Connect with exponential backoff; verify paper; wire reconnect handler."""
    ib = IB()
    delay = 2.0
    last_err = None
    for attempt in range(1, config.CONNECT_RETRIES + 1):
        try:
            log.info("connect attempt %d -> %s:%d clientId=%d",
                     attempt, config.IB_HOST, config.IB_PORT, config.IB_CLIENT_ID)
            await ib.connectAsync(config.IB_HOST, config.IB_PORT,
                                  clientId=config.IB_CLIENT_ID,
                                  timeout=config.CONNECT_TIMEOUT)
            if ib.isConnected():
                break
        except Exception as e:  # noqa: BLE001 - any connect failure retries
            last_err = e
            log.warning("connect attempt %d failed: %s (retry in %.0fs)",
                        attempt, e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
    else:
        raise SystemExit(f"could not connect to IBKR after {config.CONNECT_RETRIES} "
                         f"tries: {last_err}")

    account = assert_paper_only(ib)

    def _on_disconnected():
        log.error("DISCONNECTED from IBKR (TWS/Gateway dropped)")

    ib.disconnectedEvent += _on_disconnected
    log.info("connected; serverVersion=%s", ib.client.serverVersion()
             if hasattr(ib.client, "serverVersion") else "?")
    return ib, account


async def ensure_connected(ib: IB) -> bool:
    """Call each loop iteration; reconnects with backoff if dropped."""
    if ib.isConnected():
        return True
    log.warning("connection lost - reconnecting")
    delay = 2.0
    for _ in range(config.CONNECT_RETRIES):
        try:
            await ib.connectAsync(config.IB_HOST, config.IB_PORT,
                                  clientId=config.IB_CLIENT_ID,
                                  timeout=config.CONNECT_TIMEOUT)
            if ib.isConnected():
                assert_paper_only(ib)  # re-verify after every reconnect
                log.info("reconnected OK")
                return True
        except Exception as e:  # noqa: BLE001
            log.warning("reconnect failed: %s (retry in %.0fs)", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
    log.error("reconnect exhausted")
    return False


def account_net_liq(ib: IB, account: str) -> float:
    """Best-effort NetLiquidation for the daily-stop denominator."""
    try:
        for av in ib.accountValues(account):
            if av.tag == "NetLiquidation" and av.currency == "USD":
                return float(av.value)
    except Exception as e:  # noqa: BLE001
        log.warning("accountValues failed: %s", e)
    log.warning("using fallback account value %.0f", config.ACCOUNT_FALLBACK)
    return config.ACCOUNT_FALLBACK

"""
v1.09 2026-10-02 [CLOSEGUARD] close() checks the ticket is still open at the
    broker before sending; a missing ticket never produces a blind order.
MT5 execution backend for the ES futures GEX algo.

Mirrors orders.OrderManager's interface (enter / place_bracket /
rebracket_breakeven / check_native_fills / close / bind_es) so main.py can
swap backends via ES_EXECUTOR=mt5. Execution goes to the user's AMP MT5
terminal, which MUST run on the same Windows machine as this process
(MetaTrader5 package requirement). Market data (GEX, quotes) still comes
from IBKR — only order flow moves to MT5.

MT5 notes vs the IBKR backend:
  - Entries are MARKET (TRADE_ACTION_DEAL) with sl/tp attached in the SAME
    request — MT5 holds SL/TP server-side, so there is no OCA group to
    manage and no pending orders to cancel on close/rebracket.
  - Fade entries lose the IBKR limit-at-the-wall semantics (market per the
    backend spec); dry-run still fills at the intended price.
  - TP1 -> breakeven restage uses TRADE_ACTION_SLTP on the open position.
  - Fills are polled (no push callbacks): check_native_fills() scans deal
    history for the position ticket and attributes closes to stop/tp1/tp2
    by price proximity.

Credentials: MT5_PATH / MT5_SERVER / MT5_LOGIN / MT5_PASSWORD / MT5_SYMBOL
come from the ENVIRONMENT ONLY (a .env file next to main.py on the VPS).
The password is NEVER logged, printed, or included in any error message.

dry_run=True: no order_send is ever called. Entries "fill" at the quote and
SL/TP are simulated by Position.update() on the 5s loop, exactly like the
IBKR backend.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import config
from strategy import Position

log = logging.getLogger("algo.mt5")
COMMISSION_RT = 5.00   # AMP demo ES all-in estimate ($/contract round-turn);
                       # reconcile against the account statement and adjust.
MAGIC = 20261001
DEVIATION_PTS = 50     # 0.50 price = 2 ES ticks (MT5 deviation is in points)
TICK = config.FUT_TICK


def _tick(px: float) -> float:
    return round(px / TICK) * TICK


def _req(name: str) -> str:
    """Required env var. The VALUE is never included in the error —
    especially not for MT5_PASSWORD."""
    v = os.getenv(name)
    if not v:
        raise RuntimeError(
            f"MT5Executor: missing required env var {name}. "
            f"Create a .env file next to main.py with "
            f"MT5_PATH / MT5_SERVER / MT5_LOGIN / MT5_PASSWORD / MT5_SYMBOL "
            f"(see README 'MT5 execution').")
    return v


class MT5Executor:
    def __init__(self, ib, account, dry_run: bool, get_quote, emit):
        # ib/account are accepted for interface parity with OrderManager and
        # ignored: data stays on IBKR, only execution moves to MT5.
        self.dry_run = dry_run
        self.get_quote = get_quote   # -> (bid, ask) | None for ES (IBKR feed)
        self.emit = emit
        self.mt5 = None
        self.symbol = None
        self.filling = None
        self.commission_rt = COMMISSION_RT
        self._tickets: dict[int, int] = {}    # id(pos) -> MT5 position ticket
        self._entry_dt: dict[int, datetime] = {}
        self._seen_deals: dict[int, set] = {}  # id(pos) -> deal tickets seen
        self._native_fills: dict[int, list] = {}

    # ---------------- connect ----------------
    async def connect(self):
        """initialize() + login() + symbol resolve. Fail fast, loud."""
        path = _req("MT5_PATH")
        server = _req("MT5_SERVER")
        login = _req("MT5_LOGIN")
        password = _req("MT5_PASSWORD")   # never logged
        try:
            import MetaTrader5 as mt5
        except ImportError:
            raise RuntimeError(
                "MT5Executor: the MetaTrader5 package is not installed. "
                "On the Windows VPS: .\\venv\\Scripts\\pip install MetaTrader5 "
                "(Windows-only; the AMP terminal must run on the same machine).")
        self.mt5 = mt5
        ok = await asyncio.to_thread(mt5.initialize, path=path)
        if not ok:
            raise RuntimeError(
                f"MT5 initialize() failed for MT5_PATH={path}: "
                f"{mt5.last_error()}. Is terminal64.exe running there?")

        symbol = os.getenv("MT5_SYMBOL")
        if not symbol:
            cands = await asyncio.to_thread(self._symbol_candidates)
            raise RuntimeError(
                "MT5_SYMBOL is not set. Pick your ES symbol "
                f"(candidates on this terminal: {cands or '(none found)'}) "
                "and put it in the .env file.")
        from mt5_symbol import resolve_tradeable_symbol
        resolved = await resolve_tradeable_symbol(mt5, symbol)
        info = await asyncio.to_thread(mt5.symbol_info, resolved)
        if info is None:
            cands = await asyncio.to_thread(self._symbol_candidates)
            raise RuntimeError(
                f"MT5 symbol '{resolved}' not found on this terminal. "
                f"Candidates: {cands or '(none found)'}")
        if not info.visible:
            await asyncio.to_thread(mt5.symbol_select, resolved, True)
        self.symbol = resolved
        fm = info.filling_mode or 0
        if fm & mt5.ORDER_FILLING_IOC:
            self.filling = mt5.ORDER_FILLING_IOC
        elif fm & mt5.ORDER_FILLING_FOK:
            self.filling = mt5.ORDER_FILLING_FOK
        else:
            self.filling = mt5.ORDER_FILLING_RETURN

        try:
            login_int = int(login)
        except ValueError:
            raise RuntimeError("MT5_LOGIN must be numeric "
                               f"(got {len(login)} chars, value hidden).")
        authed = await asyncio.to_thread(
            mt5.login, login_int, password=password, server=server)
        if not authed:
            # NOTE: password never appears here.
            raise RuntimeError(
                f"MT5 login failed for login={login} server={server}: "
                f"{mt5.last_error()}. Check MT5_LOGIN/MT5_PASSWORD/MT5_SERVER.")
        acct = await asyncio.to_thread(mt5.account_info)
        await self.emit("MT5_CONNECTED", {
            "login": login, "server": server, "symbol": resolved,
            "requested": symbol,
            "balance": round(acct.balance, 2) if acct else None,
            "trade_allowed": bool((await asyncio.to_thread(
                mt5.terminal_info)).trade_allowed)})
        log.info("MT5 connected: login=%s server=%s symbol=%s (requested %s)",
                 login, server, resolved, symbol)

    def _symbol_candidates(self):
        out = []
        for pat in ("*ES*", "*EP*"):
            try:
                for s in self.mt5.symbols_get(pat) or ():
                    out.append(s.name)
            except Exception:
                pass
        return ", ".join(sorted(set(out))[:20])

    def shutdown(self):
        try:
            if self.mt5:
                self.mt5.shutdown()
        except Exception:
            pass

    def bind_es(self, contract):
        """No-op: the MT5 symbol comes from MT5_SYMBOL, not an IBKR contract.
        Kept for interface parity with OrderManager."""

    # ---------------- entries ----------------
    async def enter(self, cand: dict, now_et: datetime):
        """Market entry with SL+TP attached in one request. Returns
        Position | None."""
        side, qty = cand["side"], cand["qty"]
        is_long = side == "long"
        stop_px = _tick(cand["wall"] + (-cand["stop_pts"] if is_long
                                       else cand["stop_pts"])
                        if cand["trigger"] == "fade" else cand["stop_px"])

        if self.dry_run:
            q = self.get_quote()
            px = _tick((q[1] if is_long else q[0]) if q else cand["entry_px"])
            await self.emit("SIM_ENTER", dict({k: cand[k] for k in
                             ("trigger", "side", "wall", "qty", "risk_usd",
                              "spot", "regime") if k in cand},
                             px=px, backend="mt5"))
            await asyncio.sleep(0.2)
            pos = Position(side, qty, px, stop_px, cand["tp1_px"],
                           cand["tp2_px"], now_et.strftime("%H:%M"),
                           cand["trigger"], cand["wall"], cand["spot"],
                           simulated=True)
            await self.emit("SIM_FILL", dict(side=side, qty=qty, px=px))
            await self.emit("SIM_BRACKET",
                            dict(stop=pos.stop_px, tp1=pos.tp1_px,
                                 tp2=pos.tp2_px))
            return pos

        mt5 = self.mt5
        tick = await asyncio.to_thread(mt5.symbol_info_tick, self.symbol)
        if tick is None:
            await self.emit("ORDER_REJECT",
                            {"why": "no-tick", "symbol": self.symbol})
            return None
        price = _tick(tick.ask if is_long else tick.bid)
        tp = cand["tp1_px"] if cand.get("tp1_px") else cand["tp2_px"]
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(qty),
            "type": mt5.ORDER_TYPE_BUY if is_long else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": _tick(stop_px),
            "tp": _tick(tp),
            "deviation": DEVIATION_PTS,
            "magic": MAGIC,
            "comment": "algo_es",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self.filling,
        }
        await self.emit("ENTER", dict(trigger=cand["trigger"], side=side,
                                      qty=qty, px=price, wall=cand["wall"],
                                      backend="mt5",
                                      request={k: v for k, v in
                                               request.items()}))
        result = await asyncio.to_thread(mt5.order_send, request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            await self.emit("ORDER_REJECT", {
                "trigger": cand["trigger"], "side": side,
                "retcode": getattr(result, "retcode", None),
                "comment": getattr(result, "comment", "order_send→None")})
            log.warning("MT5 entry rejected: retcode=%s comment=%s",
                        getattr(result, "retcode", None),
                        getattr(result, "comment", None))
            return None
        filled = int(round(result.volume or 0))
        avg = float(result.price or price)
        if filled <= 0:
            await self.emit("ORDER_REJECT",
                            {"why": "zero-fill", "retcode": result.retcode})
            return None
        await self.emit("FILL", dict(side=side, qty=filled, px=round(avg, 2)))
        pos = Position(side, filled, avg, stop_px, cand["tp1_px"],
                       cand["tp2_px"], now_et.strftime("%H:%M"),
                       cand["trigger"], cand["wall"], cand["spot"],
                       simulated=False)
        ticket = await self._find_position_ticket(qty)
        if ticket:
            self._tickets[id(pos)] = ticket
            self._entry_dt[id(pos)] = datetime.now(timezone.utc)
            self._seen_deals[id(pos)] = set()
        await self.emit("BRACKET", dict(stop=round(pos.stop_px, 2),
                                        tp1=pos.tp1_px, tp2=pos.tp2_px,
                                        qty=pos.qty, ticket=ticket))
        return pos

    async def _find_position_ticket(self, qty: int):
        """Position ticket for the just-opened trade: prefer a fresh ticket
        carrying our magic, else any ticket not already tracked."""
        try:
            poss = await asyncio.to_thread(
                self.mt5.positions_get, symbol=self.symbol) or ()
        except Exception:
            return None
        known = set(self._tickets.values())
        fresh = [p for p in poss
                 if p.ticket not in known and p.magic == MAGIC]
        if fresh:
            return fresh[-1].ticket
        rest = [p for p in poss if p.ticket not in known]
        return rest[-1].ticket if rest else None

    # ---------------- brackets (SL/TP live server-side on MT5) ----------------
    async def place_bracket(self, pos: Position):
        """No-op on MT5: SL/TP were attached to the entry request and live
        on the trade server. Emits BRACKET for journal parity."""
        if self.dry_run:
            await self.emit("SIM_BRACKET",
                            dict(stop=pos.stop_px, tp1=pos.tp1_px,
                                 tp2=pos.tp2_px))
            return
        await self.emit("BRACKET", dict(stop=round(pos.stop_px, 2),
                                        tp1=pos.tp1_px, tp2=pos.tp2_px,
                                        qty=pos.qty, note="sl/tp-native"))

    async def rebracket_breakeven(self, pos: Position):
        """TP1 filled: move stop to breakeven, TP to tp2 (TRADE_ACTION_SLTP)."""
        if self.dry_run:
            pos.move_stop_to_breakeven()
            await self.emit("SIM_REBRACKET",
                            dict(stop=pos.stop_px, tp2=pos.tp2_px,
                                 runner_qty=pos.tp2_qty))
            return
        ticket = self._tickets.get(id(pos))
        if not ticket:
            log.warning("rebracket: no MT5 ticket for position")
            return
        pos.move_stop_to_breakeven()
        request = {
            "action": self.mt5.TRADE_ACTION_SLTP,
            "symbol": self.symbol,
            "position_id": ticket,
            "sl": _tick(pos.stop_px),
            "tp": _tick(pos.tp2_px),
            "magic": MAGIC,
        }
        result = await asyncio.to_thread(self.mt5.order_send, request)
        if result is None or result.retcode != self.mt5.TRADE_RETCODE_DONE:
            await self.emit("ORDER_REJECT", {
                "what": "rebracket",
                "retcode": getattr(result, "retcode", None),
                "comment": getattr(result, "comment", "order_send→None")})
            log.warning("MT5 rebracket rejected: %s",
                        getattr(result, "comment", None))
            return
        await self.emit("REBRACKET", dict(stop=round(pos.stop_px, 2),
                                          tp2=pos.tp2_px,
                                          runner_qty=pos.tp2_qty))

    # ---------------- polled native fills ----------------
    def _attribute_leg(self, pos: Position, px: float) -> str:
        tol = TICK * 2
        cands = [("stop", pos.stop_px)]
        if pos.tp1_px and pos.tp1_qty > 0:
            cands.append(("tp1", pos.tp1_px))
        if pos.tp2_px and pos.tp2_qty > 0:
            cands.append(("tp2", pos.tp2_px))
        leg, lvl = min(cands, key=lambda c: abs(c[1] - px))
        return leg if abs(lvl - px) <= tol else "exit"

    def check_native_fills(self, pos: Position):
        """Poll MT5 deal history for closes on this position's ticket.
        Returns [(leg, qty, px)] like OrderManager."""
        if self.dry_run:
            return self._native_fills.pop(id(pos), [])
        ticket = self._tickets.get(id(pos))
        if not ticket or not self.mt5:
            return []
        t0 = self._entry_dt.get(id(pos),
                                datetime.now(timezone.utc) - timedelta(hours=1))
        try:
            deals = self.mt5.history_deals_get(
                t0 - timedelta(minutes=1),
                datetime.now(timezone.utc) + timedelta(minutes=1)) or ()
        except Exception as e:
            log.warning("history_deals_get failed: %s", e)
            return []
        seen = self._seen_deals.setdefault(id(pos), set())
        out = []
        for d in deals:
            if d.position_id != ticket or d.ticket in seen:
                continue
            if d.entry not in (self.mt5.DEAL_ENTRY_OUT,
                               self.mt5.DEAL_ENTRY_INOUT):
                continue
            if d.symbol != self.symbol:
                continue
            seen.add(d.ticket)
            qty = int(round(d.volume or 0))
            if qty <= 0:
                continue
            leg = self._attribute_leg(pos, float(d.price))
            out.append((leg, qty, round(float(d.price), 2)))
            log.info("mt5 native %s filled: %s %d @ %.2f", leg, pos.side,
                     qty, d.price)
        return out

    # ---------------- exits ----------------
    async def close(self, pos: Position, reason: str, leg: str = "exit") -> float:
        """Opposite market order against the position ticket (no pending
        orders exist on MT5 — SL/TP are position properties)."""
        if pos.qty <= 0:
            return 0.0
        if self.dry_run:
            q = self.get_quote()
            px = (q[0] if pos.side == "long" else q[1]) if q else pos.entry_px
            await self.emit("SIM_EXIT", dict(side=pos.side, qty=pos.qty,
                                             px=round(px, 2), reason=reason))
            await asyncio.sleep(0.2)
            qty = pos.qty
            pnl = pos.on_fill(qty, px, leg)
            pnl -= self.commission_rt * qty
            pos.qty = 0
            return pnl
        mt5 = self.mt5
        ticket = self._tickets.get(id(pos))
        # [v1.09 CLOSEGUARD] never send a "close" for a trade the broker no
        # longer holds: on a netting account that order would OPEN a new
        # opposite position.
        if ticket:
            try:
                still = await asyncio.to_thread(mt5.positions_get, ticket=ticket)
            except Exception:  # noqa: BLE001
                still = None
            if still is not None and len(still) == 0:
                await self.emit("CLOSE_SKIPPED", {
                    "reason": reason, "ticket": ticket,
                    "note": "already closed at the broker -- no order sent"})
                pos.qty = 0
                self._tickets.pop(id(pos), None)
                return 0.0
        else:
            await self.emit("CLOSE_SKIPPED", {
                "reason": reason, "ticket": None,
                "note": "no broker ticket known -- no blind close sent"})
            log.error("close(%s): no ticket for position; check MT5 by hand", reason)
            return 0.0
        tick = await asyncio.to_thread(mt5.symbol_info_tick, self.symbol)
        is_long = pos.side == "long"
        price = _tick((tick.bid if is_long else tick.ask)
                      if tick else pos.entry_px)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(pos.qty),
            "type": mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY,
            "position_id": ticket,
            "price": price,
            "deviation": DEVIATION_PTS,
            "magic": MAGIC,
            "comment": f"algo_es:{reason}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self.filling,
        }
        await self.emit("EXIT_ORDER", dict(side=pos.side, qty=pos.qty,
                                           reason=reason, ticket=ticket))
        result = await asyncio.to_thread(mt5.order_send, request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            await self.emit("ORDER_REJECT", {
                "what": "close", "reason": reason,
                "retcode": getattr(result, "retcode", None),
                "comment": getattr(result, "comment", "order_send→None")})
            log.warning("MT5 close rejected: %s",
                        getattr(result, "comment", None))
            return 0.0
        filled = int(round(result.volume or 0))
        avg = float(result.price or price)
        if not filled:
            return 0.0
        pnl = pos.on_fill(filled, avg, leg)
        pnl -= self.commission_rt * filled
        pos.qty = 0
        self._tickets.pop(id(pos), None)
        return pnl

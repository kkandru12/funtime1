"""Order manager (v2): entries (limit at ask + one $0.05 chase), RESTING native
10x take-profit placed AT FILL, 30% giveback trail after 10x touch,
EOD flatten. The resting TP is a native exchange order: it survives a
process crash. The trail is software-managed on the 5s loop.

v1.04 2026-10-01 [SPREAD10X] 10-pt debit spreads as IBKR BAG combos:
  enter_spread (limit at the natural debit + one $0.05 chase), a native
  resting combo SELL at the cap (95% of width), and spread-aware close /
  flatten / mark.  Fills read from orderStatus (combo executions report
  per-leg fills, so summing trade.fills would double count).

dry_run=True: no orders touch IBKR. Fills are simulated at the intended
price so the full entry->exit state machine is exercised end to end.
"""
import asyncio
import logging
from datetime import datetime

from ib_insync import ComboLeg, Contract, LimitOrder, Option

import config
import spreads
from strategy import Position

log = logging.getLogger("algo.orders")
COMMISSION = 1.30


class OrderManager:
    def __init__(self, ib, account: str, dry_run: bool, get_quote, emit):
        self.ib = ib
        self.account = account
        self.dry_run = dry_run
        self.get_quote = get_quote   # key -> (bid, ask) | None
        self.emit = emit            # async json-log fn
        self.open_trades = []
        self._tp_order = {}         # id(pos) -> list of resting-TP Trades (live)
        self._tp_done = {}          # id(pos) -> (filled_qty, avg_px), legacy single TP
        self._tier_done = {}        # id(pos) -> [(tier, filled_qty, avg_px)]

    # ---------------- entries ----------------
    async def enter(self, cand: dict, qty: int, now_et: datetime):
        key, ask = cand["key"], cand["ask"]
        contract = self._contract(key)
        if self.dry_run:
            await self.emit("SIM_ENTER", dict(key=key, qty=qty, px=ask,
                                              strike=cand["strike"], right=cand["right"],
                                              window=cand["window"], otm=cand["otm"],
                                              ask_z=(round(cand["ask_z"], 2)
                                                     if cand.get("ask_z") is not None else None),
                                              trigger=cand.get("trigger", "crush")))
            await asyncio.sleep(0.2)
            pos = Position(key, cand["strike"], cand["right"], qty, ask,
                           cand["wall"], now_et.strftime("%H:%M"),
                           cand["window"], cand["spot"],
                           trigger=cand.get("trigger", "crush"),
                           simulated=True)
            await self.emit("SIM_FILL", dict(key=key, qty=qty, px=ask, side="BUY"))
            await self.place_resting_tp(pos)   # dry-run: logs the simulated TPs
            return pos

        px = ask
        order = LimitOrder("BUY", qty, px)
        order.account = self.account
        trade = self.ib.placeOrder(contract, order)
        self.open_trades.append(trade)
        filled_evt = asyncio.Event()
        trade.fillEvent += lambda t, f: filled_evt.set()
        await self.emit("ENTER", dict(key=key, qty=qty, px=round(px, 2),
                                      strike=cand["strike"], right=cand["right"],
                                      window=cand["window"], otm=cand["otm"],
                                      trigger=cand.get("trigger", "crush")))
        chased = False
        deadline = asyncio.get_event_loop().time() + config.ENTRY_GIVEUP_TIMEOUT
        chase_at = asyncio.get_event_loop().time() + config.ENTRY_FILL_TIMEOUT
        while True:
            try:
                await asyncio.wait_for(filled_evt.wait(),
                                       timeout=max(0, chase_at - asyncio.get_event_loop().time()))
                break
            except asyncio.TimeoutError:
                pass
            filled = sum(f.shares for f in trade.fills)
            if filled >= qty:
                break
            now = asyncio.get_event_loop().time()
            if not chased and now >= chase_at:
                q = self.get_quote(key)
                # chase cap: never above ask + $0.05 (their CHASECAP lesson)
                new_px = round(min((q[1] if q else px) + config.CHASE_ALLOWANCE,
                                   ask + config.CHASE_ALLOWANCE), 2)
                await self.emit("ENTER_CHASE", dict(key=key, old_px=px, new_px=new_px))
                self.ib.cancelOrder(trade.order)
                order = LimitOrder("BUY", qty - filled, new_px)
                order.account = self.account
                trade = self.ib.placeOrder(contract, order)
                trade.fillEvent += lambda t, f: filled_evt.set()
                filled_evt.clear()
                chased, px = True, new_px
                chase_at = float("inf")
            if now >= deadline:
                break
        filled = sum(f.shares for f in trade.fills)
        if trade.orderStatus.status not in ("Filled",):
            self.ib.cancelOrder(trade.order)
        if filled <= 0:
            await self.emit("ENTER_UNFILLED", dict(key=key))
            return None
        avg = sum(f.shares * f.price for f in trade.fills) / filled
        await self.emit("FILL", dict(key=key, qty=filled, px=round(avg, 2), side="BUY"))
        pos = Position(key, cand["strike"], cand["right"], filled, avg,
                       cand["wall"], now_et.strftime("%H:%M"),
                       cand["window"], cand["spot"],
                       trigger=cand.get("trigger", "crush"),
                       simulated=False)
        await self.place_resting_tp(pos)
        return pos

    # ---------------- spreads [v1.04 SPREAD10X] ----------------
    def mark(self, pos) -> float:
        """Exit-side value: spread natural (long bid - short ask) or option bid."""
        if getattr(pos, "is_spread", False):
            return spreads.mark(pos, self.get_quote)
        q = self.get_quote(pos.key)
        return q[0] if q and q[0] else 0.0

    def _bag(self, long_key: str, short_key: str):
        lc, sc = self._contracts[long_key], self._contracts[short_key]
        bag = Contract(secType="BAG", symbol=lc.symbol, currency="USD",
                       exchange="SMART")
        bag.comboLegs = [ComboLeg(conId=lc.conId, ratio=1, action="BUY",
                                  exchange="SMART"),
                         ComboLeg(conId=sc.conId, ratio=1, action="SELL",
                                  exchange="SMART")]
        return bag

    async def _work_combo(self, bag, action: str, qty: int, px: float,
                          chase: float, label: str, key: str):
        """Place a combo limit, one chase after ENTRY_FILL_TIMEOUT, give up at
        ENTRY_GIVEUP_TIMEOUT. Returns (filled, avg)."""
        order = LimitOrder(action, qty, round(px, 2))
        order.account = self.account
        trade = self.ib.placeOrder(bag, order)
        self.open_trades.append(trade)
        loop = asyncio.get_event_loop()
        chase_at = loop.time() + config.ENTRY_FILL_TIMEOUT
        deadline = loop.time() + config.ENTRY_GIVEUP_TIMEOUT
        chased = False
        while True:
            await asyncio.sleep(1.0)
            st = trade.orderStatus
            if st.status == "Filled" or st.filled >= qty:
                break
            now = loop.time()
            if not chased and now >= chase_at and chase:
                new_px = round(px + chase, 2)
                await self.emit(label + "_CHASE", dict(key=key, old_px=px, new_px=new_px))
                trade.order.lmtPrice = new_px
                self.ib.placeOrder(bag, trade.order)      # modify in place
                chased, px = True, new_px
            if now >= deadline:
                break
        st = trade.orderStatus
        if st.status != "Filled":
            try:
                self.ib.cancelOrder(trade.order)
            except Exception:
                pass
        return int(st.filled or 0), float(st.avgFillPrice or 0.0)

    async def enter_spread(self, sp: dict, qty: int, now_et: datetime):
        debit = sp["debit"]
        info = dict(key=sp["key"], qty=qty, debit=debit, width=sp["width"],
                    max_mult=sp["max_mult"], long=sp["long_key"],
                    short=sp["short_key"], window=sp.get("window"),
                    otm=sp.get("otm"), trigger=sp.get("trigger", "crush"))
        if self.dry_run:
            await self.emit("SIM_ENTER", dict(info, vehicle="spread"))
            await asyncio.sleep(0.2)
            pos = spreads.SpreadPosition(sp, qty, debit,
                                         now_et.strftime("%H:%M"), simulated=True)
            await self.emit("SIM_FILL", dict(key=pos.key, qty=qty, px=debit, side="BUY"))
            await self.place_spread_cap(pos)
            return pos
        bag = self._bag(sp["long_key"], sp["short_key"])
        await self.emit("ENTER", dict(info, vehicle="spread"))
        # chase capped: never above the 10X affordability line
        chase = min(config.CHASE_ALLOWANCE,
                    max(0.0, sp["width"] / config.SPREAD_MIN_MULT - debit))
        filled, avg = await self._work_combo(bag, "BUY", qty, debit, chase,
                                             "ENTER", sp["key"])
        if filled <= 0:
            await self.emit("ENTER_UNFILLED", dict(key=sp["key"]))
            return None
        await self.emit("FILL", dict(key=sp["key"], qty=filled, px=round(avg, 2),
                                     side="BUY", vehicle="spread"))
        pos = spreads.SpreadPosition(sp, filled, avg or debit,
                                     now_et.strftime("%H:%M"), simulated=False)
        await self.place_spread_cap(pos)
        return pos

    async def place_spread_cap(self, pos):
        """Native resting combo SELL at the cap (95% of width), day order."""
        if self.dry_run:
            await self.emit("SIM_RESTING_TP", dict(key=pos.key, qty=pos.qty,
                                                   px=pos.cap_px, vehicle="spread",
                                                   lock_10x=pos.lock_px))
            return
        bag = self._bag(pos.long_key, pos.short_key)
        order = LimitOrder("SELL", pos.qty, pos.cap_px)
        order.account = self.account
        order.tif = "DAY"
        trade = self.ib.placeOrder(bag, order)
        self.open_trades.append(trade)

        def _done(t, *_):
            st = t.orderStatus
            if st.filled:
                self._tp_done[id(pos)] = (int(st.filled), float(st.avgFillPrice or pos.cap_px))
        trade.filledEvent += _done
        self._tp_order[id(pos)] = [trade]
        await self.emit("RESTING_TP", dict(key=pos.key, qty=pos.qty, px=pos.cap_px,
                                           vehicle="spread", lock_10x=pos.lock_px))

    async def _sell_spread(self, pos, qty: int, reason: str):
        px = round(max(self.mark(pos), 0.05), 2)
        if self.dry_run:
            await self.emit("SIM_EXIT", dict(key=pos.key, qty=qty, px=px, reason=reason,
                                             vehicle="spread", mfe=round(pos.mfe, 3),
                                             mae=round(pos.mae, 3)))
            await asyncio.sleep(0.2)
            return qty, px
        bag = self._bag(pos.long_key, pos.short_key)
        await self.emit("EXIT_ORDER", dict(key=pos.key, qty=qty, px=px,
                                           reason=reason, vehicle="spread"))
        filled, avg = await self._work_combo(bag, "SELL", qty, px, -0.05,
                                             "EXIT", pos.key)
        if filled:
            await self.emit("FILL", dict(key=pos.key, qty=filled, px=round(avg, 2),
                                         side="SELL", reason=reason, vehicle="spread"))
        else:
            await self.emit("EXIT_WORKING", dict(key=pos.key, qty=qty, px=px))
        return filled, avg

    # ---------------- resting TPs (v3: tiered) ----------------
    async def place_resting_tp(self, pos: Position):
        """Native SELL limit(s) placed at fill. Survive process death.

        Tiered (default): T1 = t1_qty @ 5x, T2 = t2_qty @ 10x; the runner
        trails. Off / too-small qty: single 10x TP on the full qty (v2).
        Dry-run: only logged; the state machine simulates the fills."""
        legs = []
        if pos.tiered_effective:
            if pos.t1_qty > 0:
                legs.append((1, pos.t1_qty, pos.tp1_px))
            if pos.t2_qty > 0:
                legs.append((2, pos.t2_qty, pos.tp2_px))
        else:
            legs.append((0, pos.qty, pos.tp_px))
        if self.dry_run:
            await self.emit("SIM_RESTING_TP",
                            dict(key=pos.key,
                                 legs=[{"tier": t, "qty": q, "px": round(p, 2)}
                                       for t, q, p in legs],
                                 runner=pos.runner_qty,
                                 tiered=pos.tiered_effective))
            return
        contract = self._contract(pos.key)
        trades = []
        for tier, qty, px in legs:
            order = LimitOrder("SELL", qty, round(px, 2))
            order.account = self.account
            order.tif = "GTC"
            trade = self.ib.placeOrder(contract, order)
            self.open_trades.append(trade)
            trades.append(trade)
            if tier == 0:
                trade.fillEvent += lambda t, f: self._on_tp_fill(pos, t)
            else:
                trade.fillEvent += lambda t, f, tier=tier: \
                    self._on_tier_fill(pos, tier, t)
        self._tp_order[id(pos)] = trades
        await self.emit("RESTING_TP",
                        dict(key=pos.key,
                             legs=[{"tier": t, "qty": q, "px": round(p, 2)}
                                   for t, q, p in legs],
                             runner=pos.runner_qty,
                             tiered=pos.tiered_effective))

    def _cancel_resting(self, pos: Position):
        for trade in self._tp_order.pop(id(pos), []):
            if not self.dry_run:
                try:
                    self.ib.cancelOrder(trade.order)
                except Exception:
                    pass

    def _on_tier_fill(self, pos: Position, tier: int, trade):
        filled = sum(f.shares for f in trade.fills)
        if filled > 0:
            avg = sum(f.shares * f.price for f in trade.fills) / filled
            self._tier_done.setdefault(id(pos), []).append((tier, filled, avg))
            log.info("resting T%d filled: %s qty=%d @ %.2f",
                     tier, pos.key, filled, avg)

    def check_tier_fills(self, pos: Position):
        """Live tier fills since last call: [(tier, filled_qty, avg_px)]."""
        return self._tier_done.pop(id(pos), [])

    def _on_tp_fill(self, pos: Position, trade):
        filled = sum(f.shares for f in trade.fills)
        if filled > 0:
            avg = sum(f.shares * f.price for f in trade.fills) / filled
            self._tp_done[id(pos)] = (filled, avg)
            log.info("resting TP filled: %s qty=%d @ %.2f", pos.key, filled, avg)

    def check_tp_fill(self, pos: Position):
        """Return (filled_qty, avg_px) if the resting TP filled, else None."""
        return self._tp_done.pop(id(pos), None)

    async def arm_trail(self, pos: Position, touch_bid: float):
        """10x touched: cancel the resting TP(s), switch remainder to the
        30% giveback trail."""
        self._cancel_resting(pos)
        pos.arm_trail(touch_bid)
        pos.trail_arm_emitted = True
        await self.emit("TRAIL_ARM", dict(key=pos.key, touch_bid=round(touch_bid, 2),
                                          floor=round(pos.trail_floor, 2),
                                          peak=round(pos.trail_peak, 2),
                                          runner_qty=pos.runner_qty,
                                          trigger=pos.trigger))

    # ---------------- exits ----------------
    async def _sell(self, pos: Position, qty: int, reason: str):
        """Marketable limit sell at current bid. Returns (filled_qty, avg_px)."""
        q = self.get_quote(pos.key)
        bid = q[0] if q and q[0] and q[0] > 0 else 0.01
        bid = round(bid, 2)
        if self.dry_run:
            await self.emit("SIM_EXIT", dict(key=pos.key, qty=qty, px=bid,
                                             reason=reason, mfe=round(pos.mfe, 3),
                                             mae=round(pos.mae, 3)))
            await asyncio.sleep(0.2)
            return qty, bid
        contract = self._contract(pos.key)
        order = LimitOrder("SELL", qty, bid)
        order.account = self.account
        trade = self.ib.placeOrder(contract, order)
        filled_evt = asyncio.Event()
        trade.fillEvent += lambda t, f: filled_evt.set()
        await self.emit("EXIT_ORDER", dict(key=pos.key, qty=qty, px=bid, reason=reason))
        try:
            await asyncio.wait_for(filled_evt.wait(), timeout=15)
        except asyncio.TimeoutError:
            # one step-down, then leave working into the close
            q2 = self.get_quote(pos.key)
            nb = round(max((q2[0] if q2 and q2[0] else bid) - 0.05, 0.01), 2)
            await self.emit("EXIT_STEPDOWN", dict(key=pos.key, old=bid, new=nb))
            self.ib.cancelOrder(trade.order)
            order = LimitOrder("SELL", qty, nb)
            order.account = self.account
            trade = self.ib.placeOrder(contract, order)
            filled_evt2 = asyncio.Event()
            trade.fillEvent += lambda t, f: filled_evt2.set()
            try:
                await asyncio.wait_for(filled_evt2.wait(), timeout=10)
            except asyncio.TimeoutError:
                await self.emit("EXIT_WORKING", dict(key=pos.key, qty=qty, px=nb))
        filled = sum(f.shares for f in trade.fills)
        avg = sum(f.shares * f.price for f in trade.fills) / filled if filled else 0.0
        if filled:
            await self.emit("FILL", dict(key=pos.key, qty=filled,
                                         px=round(avg, 2), side="SELL", reason=reason))
        return filled, avg

    async def close(self, pos: Position, reason: str):
        """Close remaining qty. Returns realized PnL incl. commission."""
        # never leave a resting TP behind a closed position
        self._cancel_resting(pos)
        self._tp_done.pop(id(pos), None)
        self._tier_done.pop(id(pos), None)
        if pos.qty <= 0:
            return 0.0
        spread = getattr(pos, "is_spread", False)
        if spread:
            filled, avg = await self._sell_spread(pos, pos.qty, reason)
        else:
            filled, avg = await self._sell(pos, pos.qty, reason)
        if filled:
            pnl = pos.on_close(filled, avg) - COMMISSION * (2 if spread else 1)
            pos.qty -= filled
            return pnl
        return 0.0

    async def flatten(self, pos: Position, reason: str):
        """EOD / kill-switch: cancel everything, sell all."""
        for t in list(self.open_trades):
            try:
                self.ib.cancelOrder(t.order)
            except Exception:
                pass
        self._tp_order.pop(id(pos), None)
        pnl = await self.close(pos, reason)
        return pnl

    def _contract(self, key):
        # resolved via bridge contracts.json descriptors at startup (set by main)
        return self._contracts[key]

    def bind_contracts(self, contracts: dict):
        self._contracts = contracts

    def bind_contract_descriptors(self, descs: dict):
        """Build ib_insync Option contracts from the bridge's
        contracts.json descriptors ({'6560C': {...}}). main() qualifies
        them once before trading."""
        contracts = {}
        for key_str, d in descs.items():
            contracts[key_str] = Option(
                symbol=d["symbol"],
                lastTradeDateOrContractMonth=d["lastTradeDateOrContractMonth"],
                strike=d["strike"], right=d["right"],
                exchange=d["exchange"], tradingClass=d.get("tradingClass") or "",
                currency=d.get("currency") or "USD",
                multiplier=d.get("multiplier") or "100")
        self._contracts = contracts
        return contracts

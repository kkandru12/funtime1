"""SQUEEZE — ported from Apex run_squeeze_cycle ("Merged Vanna+Charm Flow Engine").

The entry trigger is dealer flow, NOT price structure:
  net_mom = signed(vanna_score) + signed(charm_score)
  EOD window   15:45-16:45 ET : |net_mom| > 0.20 -> EOD_Ramp
  otherwise                   : |net_mom| > 0.35 -> Dealer_Unwind (buy)
                                                 / Dealer_Hedge (sell)

  SL: 20 pts (15 EOD).  TP: GEX call/put wall, fallback +/-40 pts (+/-30 EOD).
  One signal per cooldown (60 s). V-shape opposite direction fires freely.

Because the signal IS the GEX feed, this module cannot run on price action
alone: state["gex"] must carry
  {"vanna_score","vanna_dir","charm_score","charm_dir",
   "is_negative_regime","is_fresh","call_wall","put_wall"}.
If state["gex"] is absent/unusable, on_bar returns None (documented, not
silent — the caller must wire the GEX context).

Stripped vs Apex: DOM gate, news-calendar gate, MT5Executor position guard
(same-direction stacking is the caller's job), GEX freshness is kept as an
explicit `require_fresh_gex` param (Apex default requires fresh).

Interface:
  bar   : newest CLOSED M1 bar dict (only its time is used for dedup)
  state : {"now_et": datetime (ET), "last_price": float,
           "gex": dict as above}
"""
from __future__ import annotations

import datetime as _dt
import logging
import time
from typing import Dict, Optional

from .signal import Signal, globex_only_ok, now_et, ts

log = logging.getLogger("algo_es.strategies.squeeze")


class Strategy:
    def __init__(self, config: Optional[Dict] = None):
        c = config or {}
        g = c.get
        self.mom_thresh = float(g("mom_thresh", 0.35))
        self.mom_thresh_eod = float(g("mom_thresh_eod", 0.20))
        self.sl_pts = float(g("sl_pts", 20.0))
        self.sl_pts_eod = float(g("sl_pts_eod", 15.0))
        self.tp_pts = float(g("tp_pts", 40.0))
        self.tp_pts_eod = float(g("tp_pts_eod", 30.0))
        self.cooldown_sec = float(g("cooldown_sec", 60.0))
        self.require_neg_gamma = bool(g("require_neg_gamma", False))
        self.require_fresh_gex = bool(g("require_fresh_gex", True))
        self.globex_only = bool(g("globex_only", True))
        self.rth_start = str(g("rth_start", "09:30"))
        self.rth_end = str(g("rth_end", "13:00"))
        self._last_fire = 0.0
        self._last_bar_t = 0.0

    def on_bar(self, bar: Dict, state: Optional[Dict] = None) -> Optional[Signal]:
        state = state or {}
        if self.globex_only and not globex_only_ok(
                state, self.rth_start, self.rth_end):
            return None

        gex: Optional[Dict] = state.get("gex")
        if not gex:
            return None                       # no GEX context: no signal
        if self.require_fresh_gex and not gex.get("is_fresh", False):
            return None

        bar_t = ts(bar.get("time"))
        if bar_t and bar_t == self._last_bar_t:
            return None
        self._last_bar_t = bar_t

        n = now_et(state).time()
        is_eod = _dt.time(15, 45) <= n <= _dt.time(16, 45)
        in_session = _dt.time(9, 30) <= n <= _dt.time(16, 0)
        is_neg = bool(gex.get("is_negative_regime", False))

        if self.require_neg_gamma and not is_eod and not (in_session and is_neg):
            return None

        v = float(gex.get("vanna_score") or 0.0) * \
            (1 if str(gex.get("vanna_dir")) == "bullish" else -1)
        ch = float(gex.get("charm_score") or 0.0) * \
            (1 if str(gex.get("charm_dir")) == "bullish" else -1)
        net_mom = v + ch

        thresh = self.mom_thresh_eod if is_eod else self.mom_thresh
        if abs(net_mom) <= thresh:
            return None
        action = "buy" if net_mom > 0 else "sell"

        now = time.time()
        if self.cooldown_sec > 0 and (now - self._last_fire) < self.cooldown_sec:
            return None
        self._last_fire = now

        last_price = float(state.get("last_price") or bar.get("close") or 0.0)
        if not last_price:
            return None

        sl_pts = self.sl_pts_eod if is_eod else self.sl_pts
        tp_pts = self.tp_pts_eod if is_eod else self.tp_pts
        if action == "buy":
            sl_px = round(last_price - sl_pts, 2)
            cw = gex.get("call_wall")
            tp_px = round(float(cw), 2) if cw else round(last_price + tp_pts, 2)
            strategy = "EOD_Ramp" if is_eod else "Dealer_Unwind"
        else:
            sl_px = round(last_price + sl_pts, 2)
            pw = gex.get("put_wall")
            tp_px = round(float(pw), 2) if pw else round(last_price - tp_pts, 2)
            strategy = "EOD_Ramp" if is_eod else "Dealer_Hedge"

        log.info("[SQUEEZE] FIRING %s %s @ %.2f | Mom=%+.2f thresh=%.2f | "
                 "SL=%.2f TP=%.2f", strategy, action.upper(), last_price,
                 net_mom, thresh, sl_px, tp_px)
        return Signal(
            side=action, entry_px=last_price, stop_px=sl_px, target_px=tp_px,
            strategy_name=strategy, confidence=0.99,
            reason=f"Vanna+Charm: {net_mom:+.2f} (v={v:+.2f} c={ch:+.2f})",
            extra={"net_mom": round(net_mom, 3), "is_eod": is_eod})

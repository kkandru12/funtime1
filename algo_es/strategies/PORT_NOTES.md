# PORT NOTES — Apex Globex strategies → `algo_es/strategies/`

Source: `~/workspace/user/files/Apextrader_ES_Options_PRE_POST_KK-v2.py` (private, unmodified).
Target: one module per strategy, each exposing
`class Strategy` with `__init__(config)` and `on_bar(bar, state) -> Signal | None`.

`bar` = newest **CLOSED** bar dict `{time, open, high, low, close}`.
`state` = dict the caller populates (documented per module below).
`Signal` (in `signal.py`): `side, entry_px, stop_px, target_px, strategy_name, reason, confidence, extra`.

## Closed-bar convention (important)

Apex evaluates on `bars.iloc[-2]` because `iloc[-1]` is the forming bar.
The port consumes **closed bars only**, so every "last closed" index shifts by one:
Apex `iloc[-2]` → port `bars[-1]`; Apex `iloc[-3]` → port `bars[-2]`.
Semantics are identical as long as the caller never feeds a forming bar
(except `dma520` m1-mode, where the forming tf candle is explicitly the last
element of `state["tf_bars"][tf]` — documented in that module).

## What was stripped from all five

- **MT5Executor** → replaced by the `Signal` dataclass. The caller executes.
- **Position guards** (`get_positions`, one-at-a-time, same-direction stacking):
  executor-level, now the caller's job. Each Strategy still resets its own
  state machine on fire (as Apex does) and keeps its cooldowns/dedup.
- **DOM gates** (`dom_force_trade` / `dom_gex_action` conflict checks in VOB
  and SQUEEZE): removed — DOM order-flow feed does not exist in the new stack.
- **News-calendar gate** (`is_news_window_active` in SQUEEZE): removed.
- **Apex threading model** (refresh threads, bar caches, locks): the caller
  owns bar feeds; VOB's 30 s zone refresh became a time-throttled recompute
  inside `on_bar`.
- **Logging**: standard `logging` per module (`algo_es.strategies.<name>`).
- **GEX client object** → optional plain-dict `state["gex"]`
  (`call_wall`, `put_wall`, plus vanna/charm fields for SQUEEZE).
- **SPX-anchored synth 5DMA** (`_daily_5dma` via server seed): replaced by
  mean of last 5 `state["daily_closes"]`, with the same Apex fallbacks.

## Per-strategy notes

### vob.py — VOB_Retest (Apex `run_vob_cycle`, L12243)
- Faithful: H1 EMA(5)/EMA(18) cross zones (port of MQ5 `VOB_Calc`), zone
  invalidation vs last H1 close, keep-last-15, trade most-recent zone only.
- Faithful: 3-state machine IDLE→TOUCHED→REVERSED→fire on closed M1 bars,
  `buf=ATR14*0.5`, proximity `ATR*2.0`, clear distance `ATR*0.1`.
- Faithful: TP = last_price ±40, SL = wall ∓10, tick-rounded.
- Changed: zone refresh thread → throttled recompute (`zone_refresh_sec=30`).
  M1 zone fallback stays OFF by default (Apex `VOB_M1_ZONE_FALLBACK=0`).
- Changed: `VOB_ONE_AT_A_TIME` position guard is caller-side now.
- Needs: `state["bars_h1"]` (≥ ~25 bars), `state["bars_m1"]` (≥15 for ATR),
  `now_et`, `last_price`.

### squeeze.py — SQUEEZE (Apex `run_squeeze_cycle`, L12387)
- Faithful: `net_mom = ±vanna_score ± charm_score`, thresholds 0.35
  (0.20 in the 15:45–16:45 ET EOD window), SL 20 (15 EOD), TP = GEX
  call/put wall else ±40 (±30 EOD), 60 s cooldown, labels
  EOD_Ramp / Dealer_Unwind / Dealer_Hedge.
- **Cannot run on price action alone** — the trigger IS the GEX feed.
  Returns `None` unless `state["gex"]` carries
  `vanna_score, vanna_dir, charm_score, charm_dir, is_negative_regime,
  is_fresh, call_wall, put_wall`.
- `SQUEEZE_REQUIRE_NEG_GAMMA=0` default kept (gate inert, as in Apex).
- `require_fresh_gex=True` default mirrors Apex (stale GEX → skip unless
  the caller relaxes it).
- Needs: `now_et`, `last_price`, `gex` dict.

### bb2c.py — BB-2C (Apex `run_consol_bb_2c_cycle`, L12020)
- Faithful: per-bar Bollinger (20, 2.0, **population** sigma to match the EA),
  A-closes-outside-own-band → fade direction, B-back-inside + correct-half
  rule (v2.73 fix).
- Faithful default `entry_trigger="first_m1"`: A = last closed structure bar,
  entry = first closed M1 of the next structure bar, refused if that M1 is
  older than 180 s. Forming-bar open time is derived as
  `A.time + struct_tf` (caller feeds closed bars only).
- Faithful two-candle branch: entry-TF pin wins outright → else FVG limit
  when B within 20 pts of 5DMA (dropped — not downgraded — when no imbalance)
  → else market at B close. TP = 5DMA capped at 40 pts (`_tp_capped`);
  SL ±20; setup dropped when 5DMA is not ahead of entry.
- 5DMA: mean of last 5 `daily_closes`, fallback SMA(5) of structure bars.
- Defaults: `struct_tf="4h"`, `entry_tf="1h"` (Apex env defaults).
- Needs: `state["bars"]` (structure, ≥23), `state["entry_bars"]` (two-candle
  pin), `state["bars_m1"]` (first_m1), `state["daily_closes"]`, `now_et`,
  `last_price`, `struct_tf`.

### dma520.py — DMA-520 (Apex `run_5_20dma_sweep`, L9837)
- Faithful: daily 5/20 SMAs from confirmed daily closes (≥20 required);
  "inside" double-sweep rule (low ≤ both, high ≥ both, close between);
  direction fades the larger overshoot; both overshoots ≥ 2.0 pts.
- Faithful: `entry_mode="m1"` default — sweep read off the FORMING tf candle,
  decision close = last closed M1 (rejected if it predates the tf candle or
  is older than 180 s); `"closed"` mode uses the last closed tf candle.
- Faithful: SL = swept extreme ±3.0 (the wick is the invalidation);
  TP = tf Bollinger band (upper for buy / lower for sell) pulled in to
  40 pts; dropped when the band is not beyond entry.
- Faithful: `"outside"` traverse mode + pin-bar gate (60/35/20) ported behind
  `close_mode="outside"` / `pin_gate` params (pin gate deliberately NOT
  applied in inside mode, as in Apex).
- First matching TF wins; cooldown 300 s; one signal per tf candle.
- Needs: `state["tf_bars"]` = `{"1h": [...], "4h": [...]}` (forming bar last
  for m1 mode), `state["bars_m1"]`, `state["daily_closes"]` (≥20), `now_et`.

### smacross.py — SMA-CROSS (Apex `run_sma_cross_cycle`, L15130)
- Faithful: M15 SMA(50)/SMA(200), cross on two closed candles, separation
  ≥ 2.0 pts, flat→long→short state machine, 900 s cooldown, SL ±25,
  TP = GEX call/put wall (only when >5 pts beyond entry) else ±60,
  R:R ≥ 1.5 gate.
- Session: Globex-only gate + the 16:59–18:00 ET maintenance-pause exclusion
  (`_apex_entry_window_open` port).
- **Note: the 2-pt separation filter makes this strategy extremely
  selective** — a cross is only detected when the SMAs are already ≥2 pts
  apart at the cross bar, which needs a ~130+ pt single-bar impulse on M15.
  The earlier 3-month backtest found 36 raw crosses, 0 passing the filter.
  Ported faithfully; selectivity is Apex's, not a port artifact.
- Needs: `state["bars"]` (M15, ≥205), `now_et`, `last_price`,
  optional `state["gex"]` = `{call_wall, put_wall}`.

## Could not be ported faithfully

1. **SQUEEZE without GEX** — the entire signal is vanna/charm dealer-flow
   scores. There is no price-action equivalent in the Apex source; the module
   requires `state["gex"]` and returns `None` without it.
2. **SMA-CROSS / BB-2C GEX-wall TPs** — degrade gracefully to the fixed-point
   fallbacks (±60 / ±40) when `state["gex"]` is absent, exactly as Apex does.
3. **VOB's GEX context** — Apex fetched it only to hand to `execute()`;
   the trigger is pure price structure, so nothing was lost.
4. **BB-2C `_daily_5dma` SPX-anchored path** — needed the Apex server's
   seeded SPX anchors; replaced by plain daily closes (the
...[truncated 1149 chars]
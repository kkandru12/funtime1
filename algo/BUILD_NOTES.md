# BUILD NOTES — 0DTE SPXW 10X-OTM algo (v2 merged)

Built 2026-10-01 (v1). Merged 2026-10-01 (v2) with the user's proven 10X-OTM
mechanics after a 5-day Databento backtest showdown (~/workspace/algo-review/
showdown.md). Single asyncio event loop, `ib_insync`, no GUI, no hardcoded
credentials (all connection settings env-overridable).

## v2 merge: what changed and why (evidence)

The showdown ran the user's 10X-OTM vs this algo's v1 crush design on the
same 5 days (2026-09-21..25 SPXW 0DTE, 1-min CBBO, buy-ask/sell-bid):

| metric | theirs (10X-OTM) | mine (v1) |
|---|---|---|
| avg max multiple | 9.08x | 1.36x |
| max multiple | 29.67x | 2.83x |
| 10x touch rate | 28.6% | 0.0% |
| P&L (native sizes) | +$8,291 | −$633 |

Window scan (filter-only, best gamma/ask candidate/min, max forward bid):

| window | theirs 10x hit | mine 10x hit |
|---|---|---|
| 11:00–12:30 | 21.4% | 17.0% |
| 13:00–15:00 | **0.0% (all 5 days, both filters)** | 0.0% |
| 13:00–15:50 | 1.3% | 1.4% |
| 15:30–15:58 | 12.4% | 3.6% |

Verdict: the v1 13:00–15:50 entry window is RETIRED (0% hit in 13:00–15:00).
The v1 *filter* was not dead (17% 10x-quality in the morning) — the window was.
Merged: their windows + exits (measured) inside our chassis (paper gate, kill
switch, $200 sizing).

## v3 (2026-10-01): three independent, config-toggled modules + always-on

All three default to the measured v2 behavior when toggled off. Each module
journals separately so its contribution can be measured in the JSONL logs.

### Module A — tiered exits (`CRUSH_TIERED_EXITS`, default on)

T1: resting native limit SELL 1/3 of the position at 5x entry. T2: resting
native limit SELL 1/3 at 10x. Runner 1/3 on the existing 30% giveback trail
machinery (TP_TOUCH→TRAIL, floor = max(10x, peak×0.70), 4-tick confirmation).
Both TPs are native GTC orders placed AT FILL (survive process death);
dry-run simulates them in the state machine. Partial fills flow through the
tiers correctly; every tier fill is journaled (`TIER_FILL`: tier, multiple,
qty, ts, trigger, regime_score). Qty < 3 falls back to the v2 single-10x-TP
path (nothing sensible to split). `CRUSH_TIERED_EXITS=0` restores pure v2
behavior. Rationale: locks the $200→$1,000/$2,000 goal earlier while keeping
the 50x tail on the runner.

### Module B — wall-break sleeve (`CRUSH_WALLBREAK_ENABLED`, default on)

Event-driven entries on dominant GEX-wall breaks, ported from the user's
`_wb_step` (WALLBRK v1.59) arm/break semantics, simplified:
- **Arm:** call_wall/put_wall (morning OI × live gamma) is dominant when its
  same-side OI ≥ `CRUSH_WALL_DOMINANCE` (2.5) × the adjacent spot-side
  strike's OI; armed when |spot − wall| ≤ `CRUSH_WALL_ARM_PTS` (15). Call
  wall arms only as resistance (spot ≤ wall → side "up"); put wall only as
  support (spot ≥ wall → side "down"). Disarms past 2× arm distance.
- **Break:** two consecutive 1-min closes beyond the wall (above call wall →
  calls; below put wall → puts). M1 closes built from chain spot exactly as
  their replay harness does (last spot seen in the minute = the close).
- **Entry:** best gamma/ask outright within `CRUSH_WALLBREAK_OTM_MAX` (30)
  pts beyond the wall; ask $0.20–$0.50, |delta| < 0.15, spread ≤ 25% —
  **no crush z-band** (breaks are momentum events, not crush events).
- **Windows (user constraint):** arming AND entries are gated on the SAME v2
  windows A/B via `config.active_window()` — no around-the-clock trading.
  Crush is evaluated first (v2 priority); wall-break is the fallback sleeve.
- **Deviations from theirs (deliberate):** our vehicle is outrights, not
  their 10-wide debit spreads; no pre-buy while pinned (break-add only);
  no ES leg; max 2 breaks/wall/day; 15:55 flat (no hold-to-settlement).
- Shared limits: 1 position total, 2/day, $200 sizing; exits run through the
  same tiered/trail machine; journal `trigger="wallbreak"` vs `"crush"`.

### Module C — regime / day filter (`CRUSH_REGIME_MODE`, default `observe`)

Morning features: overnight gap % (prev daily close → first spot, via
`reqHistoricalData`), first-30-min range % (1-min spot closes), VIX level
(+ day change when history is available; snapshot request, no line held),
day of week. Combined into `regime_score ∈ [0,1]` with placeholder weights
(gap .35 / range .35 / vix .30; gap normalized at 1%, range at 1.5%, VIX
12→0 / 30→1). `REGIME_SNAPSHOT` + `REGIME` events journal features, score,
and what was unavailable.
- `observe` (default): log only, zero effect on trading.
- `live`: score < `CRUSH_REGIME_MIN_SCORE` (0.35) → no new entries that day
  (journaled `REGIME_GATE`); score ≥ `CRUSH_REGIME_HIGH_SCORE` (0.70) →
  1.5× size, hard-capped at `CRUSH_REGIME_TRADE_CAP` ($300)/trade.

### Always-on (default) / `--oneshot`

`main.py` is now a supervisor: `run_session()` runs one full session
(connect → chain → OI snapshot → trade → 15:55 flatten → 16:05 disconnect),
then the supervisor sleeps — interruptibly (returns instantly on Ctrl+C /
SIGTERM; 60s chunks otherwise) — until the next weekday 09:30 ET and runs
the session fresh. NO_TRADE_DAY (weekend / past 16:05 / empty chain) just
sleeps to the next session; the process never exits on its own.
- `--oneshot` restores the old run-once-then-exit behavior (debugging).
- Per-session hygiene: fresh `IB()` connect, chain discovery, OI snapshot,
  `RiskManager`, and a new dated log file each session (old handlers are
  dropped — no double logging).
- Crash policy: unexpected exception → `SESSION_ERROR` journaled, 5-min
  interruptible sleep, fresh session retry. `SystemExit` (paper-gate refusal
  / connect exhaustion — needs a human or TWS) → `SESSION_ABORT`, sleep to
  next session.
- 15:55 flatten and 16:05 loop-end are unchanged within a session.
- Task Scheduler/systemd is now optional: only needed as a watchdog to
  restart the process after a VPS reboot (see README §4).

### Journal schema additions (v3)

Every `CANDIDATE`/`CLOSED` now carries `trigger` (`crush`/`wallbreak`) and
`regime_score`. New events: `TIER_FILL`, `WB_ARM`, `WB_DISARM`, `WB_BREAK`,
`TRAIL_ARM` (now with `runner_qty`), `REGIME_SNAPSHOT`, `REGIME`,
`REGIME_GATE`, `SLEEP`, `DAEMON`, `DAEMON_STOP`, `SESSION_ERROR`,
`SESSION_ABORT`.

### Open work

1. **Regime calibration:** weights are placeholders. Needs 20+ journaled
   live days (regime_score + per-day P&L) before `CRUSH_REGIME_MODE=live`
   is trustworthy. Analysis: correlate score terciles with day P&L and
   10x-hit rate; refit weights; only then enable.
2. **Wall-break measurement:** the sleeve is a faithful port of their
   trigger, but its hit rate on OUR outright vehicle is unmeasured —
   journal `trigger` split for a month, then decide keep/tune/kill.
3. **Tier sizing:** 1/3-1/3-1/3 is a starting split; the journal's
   `TIER_FILL` multiples will show whether T1 at 5x leaves too much on
   the table vs pure 10x+trail.

## Files

| File | Role |
|---|---|
| `config.py` | Every parameter in one place; `CRUSH_*` env overrides; `active_window()` |
| `pacer.py` | Token-bucket API pacer (3 snapshot/sub ops/sec, max 8 concurrent) |
| `ibkr_conn.py` | Connect w/ exponential backoff, PAPER-ONLY gate, auto-reconnect |
| `chain.py` | Chain discovery, OI snapshot, line-budgeted streaming, re-centering, wing sweeps, 1-min bars, AllLast flow |
| `gex.py` | Net GEX / flip / magnets / walls from frozen OI × live IBKR gamma (incremental). **Bug fixed 2026-10-01: put_wall used min() instead of max()** |
| `strategy.py` | 10X-OTM entry filter + 10x/trail exit state machine (pure functions). v3: tiered Position (T1 5x / T2 10x / runner trail), WallBreakState (dominant-wall arm/break) |
| `regime.py` | **v3 Module C:** morning gap/VIX/range snapshot + regime_score (uncalibrated scaffolding) |
| `orders.py` | Entries (limit + $0.05 chase cap), resting native TPs at fill (v3: T1 5x + T2 10x legs, or single 10x when tiers off), trail arming, dry-run simulation |
| `risk.py` | Pre-trade checks, $200 sizing, 2%/day kill switch |
| `main.py` | Wiring: connect → OI → stream → 5s loop → 15:55 flatten → shutdown |
| `requirements.txt` | `ib_insync>=0.9.86`, `numpy==2.2.6`, `tzdata` (win32) |

## Strategy parameters (v2 — 10X-OTM, measured)

- Entry windows: **A 11:00–12:30 ET (40–55 pts OTM)** and **B 15:30–15:58 ET
  (8–12 pts OTM)**; no entries at/after 15:50; flat 15:55.
- Entry: ask $0.20–$0.50, |delta| < 0.15 (IBKR-sent Greeks), spread ≤ 25%,
  **crush BAND: ask z-score ∈ [−2.0, −1.0]** vs trailing 30-min (deeper than
  −2.0 measured worse than random — dying, not mispriced), ATM IV not spiking
  (>1.3× trailing median = stand down), rank gamma/ask, min 10 min history.
- Exits: **resting 10x limit TP placed AT FILL** (native order in live mode —
  survives process death); on 10x touch → cancel TP, arm **30% giveback trail**
  (floor ratchets at max(10x entry, peak × 0.70), ~20s confirmation on the 5s
  loop); no per-trade stop by default (`STOP_PCT=0`; their measurement: removing
  the −50% stop added $37,100/5 sessions); daily kill switch is the backstop.
- Risk: $200/trade → qty = 200 // (ask×100); max 2 trades/day; 1 position;
  −2% of NetLiq daily kill switch (flattens the open position).

## Deliberately NOT ported from their system (and why)

1. **$3,000/day premium cap / 10 contracts per ticket** — 15× the user's $200
   budget. Kept $200/trade, max 2/day.
2. **Hold-to-settlement for 10x+ positions** — keep the 15:55 hard flat.
   Settlement mechanics add pin/expiry risk; the trail already captured
   16.67x/10.6x exits in the sim vs 10x flat.
3. **Max 3 open positions / 3 per window** — kept 1 position (their own
   concentration warning: 91% of 5-day P&L from one day; 4/7 trades −100%).
4. **WALL-BREAK strategy** — phase 2 (needs ladder-monotonicity sanity +
   combo-tick machinery their v1.61 added).
5. **ZDTE-3PM** — dead design (delta 0.15–0.35 + breakout confirmation),
   verified twice (13 trades / 1 win / −$905.60; their v1.51: excludes 94% of
   10x tickets).
6. **ES→SPXW bridge** — gated off in their system; ES comes later.
7. **|iv_z| ≤ 1.5 formulation** — kept our ATM-IV-ratio gate (same spirit,
   already implemented).
8. **Absolute $0.50 spread cap** — kept our relative 25% (tighter on cheap
   tickets).
9. **45-min warmup** — kept MIN_ASK_HISTORY=10 (theirs: min 15 samples).

## Line-budget design (hard cap 100, target ≤75 sustained)

IBKR's 100-line limit is shared across `reqMktData` and `reqTickByTickData`.
Sustained layout:

| Lines | Use | Priority |
|---|---|---|
| 1 | SPX underlying streaming | 0 (never shed) |
| 42 | Active window: 5-pt strikes, spot ±50, both rights, streaming (`100,101,106,107`) | 1 |
| 20 | Crush band: 55–95 pts OTM, 10-pt steps, both sides, both rights, streaming | 2 |
| ≤10 | AllLast tick-by-tick: held position + top-3 candidates (flow/ISO context) | 3 (shed first) |
| **63** | **sustained typical** | |
| **≤73** | **peak with AllLast** | |

- **OI is frozen intraday (verified):** one full-chain OI snapshot at startup via
  paced one-shot snapshot requests (`genericTickList="101"`), then unsubscribe —
  zero streaming lines held for wings. GEX = frozen morning OI × live gamma.
- **Dynamic window:** every 15 min the active/crush sets re-center on spot;
  entering strikes subscribe, departed ones unsubscribe (all pre-qualified from
  the single morning `reqContractDetails`, so re-centering is subscribe-only).
- **Wing quotes:** every 15 min a paced snapshot sweep of non-streaming strikes
  within ±200 pts refreshes Greeks for GEX completeness. Wings never stream.
- **Shedding order near the cap:** AllLast → crush band → active window → (never SPX).
- **Compute:** IBKR-sent Greeks (`bidGreeks`/`askGreeks` from ticks 106/107) are
  used directly — no per-tick Python Greek recomputation. The crush detector
  aggregates streaming asks into 1-min bars; z-scores run on the trailing
  30-min series. GEX recomputes a strike only when its gamma moved >5%, plus a
  full refresh every 5 min.
- **Pacer:** every snapshot, subscribe, and re-center op goes through the token
  bucket (3/sec, max 8 concurrent) — sweeps and re-centering cannot trip pacing
  violations.
- **AllLast flow:** prints drained each loop, tick-rule signed vs the streaming
  quote, 5-min signed volume kept per contract. It is a logged tiebreak in
  ranking, not an entry gate (the backtest validated crush entries standalone).

## Assumptions / risks

1. **Paper gate = "DU" account prefix.** Standard for IBKR individual paper
   accounts. If the user's paper account has a different prefix, the algo will
   refuse to start — check `managedAccounts()` and adjust if needed.
2. **Chain discovery via one `reqContractDetails`** on a bare SPXW 0DTE Option
   contract. If IBKR returns an empty list (holiday, bad session), the algo
   exits with NO_TRADE_DAY.
3. **Exits are software-managed** (marketable limit orders), not native stop
   orders. If the process dies mid-position, nothing protects it — run under
   systemd with `Restart=on-failure`, and consider IBKR's own "cancel on
   disconnect" safeguards as a backstop (not configured by this algo).
4. **Snapshot fills are best-effort:** a snapshot that returns no Greeks/OI is
   logged and skipped; the morning OI snapshot needs decent coverage or GEX
   walls degrade (coverage % is logged).
5. **Conflated quotes (~4/sec)** mean entry/exit prices can be ~250ms stale;
   the strategy is bar-based and this was verified immaterial.
6. **Dry-run simulates fills at the intended price** — it exercises the state
   machine but says nothing about real fill quality. Expect slippage on real
   (paper) fills, especially the 15:55 flatten.
7. **Weekend/holiday handling** is minimal: no 0DTE chain → clean exit.
8. **Target 10x is aspirational per-trade asymmetry**, not a daily guarantee:
   verified base rate is ~2.4% of cheap tickets hitting 10x; the filter (crush +
   IV-flat + wall proximity) is what must lift it. Paper-trade for weeks and
   measure before trusting any expectancy claim.

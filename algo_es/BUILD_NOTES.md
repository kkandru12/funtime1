# BUILD_NOTES — algo_es

## 1. Why walls bounce (diagnosis)

The naive approach — `argmax` over instantaneous per-strike gamma, recomputed
every loop — produces walls that derive, bounce, and jump. Three compounding
causes:

1. **argmax over instantaneous gamma flickers.** Two adjacent strikes with
   similar GEX swap the lead every tick: bid/ask bounce → model-gamma
   jitter → the "wall" jumps strike-to-strike on pure noise.
2. **Gamma is price-dependent.** As spot approaches a strike its gamma
   inflates, so raw walls *chase price* instead of marking structure. The
   wall follows the market rather than the market respecting the wall.
3. **Tick-cadence recomputation** turns (1)+(2) into visible wall jumps.
   Walls are structural supply/demand concentrations; recomputing them at
   5-second cadence is a category error.

Their old server's `GEX_WALL_MODE=stable` was a patch on the same argmax;
the flicker is structural to the estimator, not the mode flag.

## 2. The StableWall estimator (`gex.py`)

- **Wall clock: 5 minutes.** `maybe_evaluate()` runs at most every
  `ES_GEX_EVAL_SEC` (300s). The 5s trade loop only *records* gamma samples
  (`note_gamma`) and *reads* the last published walls. Smoke T1 proves 48
  jittered ticks fire zero evaluations and move no wall.
- **gamma_TWAP15 × frozen OI → dollar gamma.** Each evaluation uses the
  15-min time-weighted average of IBKR-sent gamma per strike (kills tick
  noise) times the hourly-frozen FOP OI: `OI × gamma × spot² × 50 / 1e9`
  ($B). Between hourly sweeps OI is frozen and gamma keeps flowing — the
  documented Globex compromise (no morning snapshot exists on a 24h
  market).
- **Strike smoothing.** `GEX_s[k] = 0.25·GEX[k−5] + 0.5·GEX[k] + 0.25·GEX[k+5]`
  on the 5-pt grid. Adjacent-strike flicker merges into one hump instead of
  two alternating argmax winners.
- **Hysteresis (Schmitt trigger).** The incumbent wall keeps status until a
  challenger exceeds **1.25×** its smoothed GEX on **3 consecutive** 5-min
  evaluations (15 min). Smoke T2: ±10%/min oscillation for 60 min → wall
  never moves. Smoke T3/T4: a 2.2× step → exactly one switch, then holds;
  removing it → exactly one switch back. Note the honest latency: TWAP15
  needs ~10 min to register a step as "strong", so a 2× step switches at
  ~25 min. Genuine regime shifts still pass; flicker never does.
- **Zone, not a line.** Published zone = contiguous strikes with
  `GEX_s > 0.70 × GEX_s[wall]` → `[zone_lo, zone_hi]`. Fade entries use the
  near zone edge (better fill than the wall strike); **break detection uses
  zone edges** — 2 consecutive 1-min closes beyond the edge (+2pt buffer).
  A wick through the wall strike is not a break (smoke T7).
- **Confidence.** `min(1, tenure_min/60) × min(1, margin/0.5)`,
  `margin = (GEX[wall]−GEX[runner-up])/GEX[runner-up]` with runner-up = best
  same-side strike *outside* the zone. Strategy gating: fade needs
  confidence ≥ **0.5**; breakout arming needs ≥ **0.35** (smoke T8/T9).
- **Flip/regime hysteresis.** Net-GEX sign must hold 3 consecutive
  evaluations before the published regime flips — same Schmitt pattern.
- Every evaluation emits `WALLS` with wall, zone, confidence, tenure,
  flip, regime, net $B, magnets.

**Backport:** this pattern ports directly to `~/workspace/algo/gex.py`
(mult=100, SPXW OI instead of FOP). Not done yet — flagged for later.

## 3. Design decisions & deviations

- **Vehicle is futures, not options.** All GEX math is only used to locate
  levels; entries/exits are ES futures ($50/pt). No FOP legs are traded.
- **FOP expiry = front Friday weekly** (Friday-preferring discovery;
  document: FOPs are American-style, weeklies expire Friday).
- **Fade TP1 = flip**, not the wall: in +gamma the flip is the magnet the
  wall fades *toward*. Stop 6 pts beyond the wall (structural invalidation),
  not a premium stop.
- **Breakout stop = 4 pts back inside from the broken zone edge** (a close
  back inside the zone = thesis dead), target = next magnet beyond the edge
  else 2× stop measured move.
- **Zone snapshot at arm time** for `WallBreakState`: break detection runs
  against the armed zone even if the estimator republishes mid-break.
- **Flatten 16:55 ET** before the 17:00–18:00 Globex halt (default on);
  no entries inside the flatten window.
- **Risk:** $200/trade → `floor(200/(stop_pts×50))`, min 1 contract;
  4 trades/day; one position; −2% NetLiq kill switch. Globex "day" =
  18:00→17:00 ET for the daily reset.
- **Chassis** (DU* paper gate, dry-run default, JSONL, heartbeat, graceful
  shutdown, always-on + `--oneshot`) copied from `~/workspace/algo/`.

## 4. Simplifications vs their server

Dropped: W2W-LQ sleeve (shadow-only on their side after losses), pre-buy
legs, ES leg split on WBES, tick-cadence recompute, `GEX_WALL_MODE` flag
(replaced by the estimator), per-strike wall lines (replaced by zones).

## 5. Smoke tests — 36/36 pass (`/tmp/smoke_es.py`, synthetic)

T1 tick cadence never re-evaluates · T2 ±10% flicker 60min → no wall move ·
T3 2.2× step → exactly one switch, holds · T4 switch-back symmetric ·
T5 zone edges · T6 confidence math · T7 breaks need closes beyond zone edge
(wick ≠ break, single close ≠ break) · T8 fade confidence gate ·
T9 breakout arm confidence gate · T10 sizing · T11 dry-run TP1/BE/stop flow.

## 6. MUST-VALIDATE against TWS (dry-run first, then paper)

1. **FOP contract specs**: weekly FOP expiries/streaming symbols on TWS
   paper — `discover_weekly()` assumes Friday weeklies exist.
2. **Generic tick 101 (OI) on FOPs**: verified on SPXW index options; FOP
   futures-options tick coverage must be confirmed (fallback: request OI
   via a second contract-details pass).
3. **ContFuture('ES','CME') rolling**: front-month rollover behavior near
   expiry week; verify `fop.es_contract` tracks the intended month.
4. **Model-gamma availability**: tick 106/107 Greeks on FOPs in paper mode;
   if Greeks lag, TWAP15 degrades gracefully (fewer samples, same clock).
5. **Zone widths in production**: if zones print wider than ~15 pts
   routinely, the 0.70 fraction is too loose for ES; tighten to 0.80.
6. **Hysteresis latency**: ~25 min to acknowledge a 2× GEX step is by
   design (structural walls), but if real regime shifts need faster
   reaction, lower `HYST_EVALS` to 2 (10 min) — do not lower the 1.25×.
7. **Overnight OI staleness**: hourly sweeps assume FOP OI refreshes
   intraday; if OCC/FOP OI only refreshes once daily, the "frozen OI"
   is just daily-frozen — still fine, gamma TWAP carries the dynamics.

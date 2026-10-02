# algo_es — ES futures GEX algo (StableWall edition, CONSUMER mode)

Trades **ES front futures** ($50/pt) off GEX levels published by **gex_bridge**
(the account's single IBKR streaming connection) via `../shared/levels.json`.
This process holds ZERO IBKR lines and makes ZERO IBKR calls: quotes and
execution come from the AMP MT5 terminal on the same machine.

Two GEX sleeves, both on by default:

- **FADE** (+gamma regime): short the call wall / long the put wall on a
  touch of the wall *zone*, TP1 = flip (half; full qty when qty==1),
  TP2 = next magnet, stop 6 pts beyond the wall, stop→breakeven after TP1.
- **BREAKOUT** (dominant wall or −gamma): a dominant wall (OI ≥ 2.5×
  adjacent) breaks on 2 consecutive 1-min closes beyond the **zone edge** →
  momentum entry, stop 4 pts back inside, target = next magnet / measured
  move.

Walls come from the bridge's **StableWall estimator** — structural, NOT
recomputed at tick frequency (the bounce/jump bug your server has). The
bridge publishes SPX walls; the algo converts to ES via the live basis
(`es_walls`) and trades them with MT5 quotes. Overnight the bridge freezes
walls (confidence decayed); breakouts then run half-size with a +0.15
confidence bar. See BUILD_NOTES.md for the StableWall diagnosis.

**Sleeve schedule** (local clock is the authority; bridge session = cross-check):
- **NY (09:30–16:00 ET):** fade + breakout off bridge levels
- **Overnight (18:00–09:00):** breakouts on frozen zones (half size) +
  PA sleeves C (ON-range fade) + D (sweep+reclaim), pure MT5 price action
- **Halt 17:00–18:00 / weekend:** flat, sleep

## Quick start (LIVE by default: MT5 demo)

```bat
cd C:\path\to\algo_es
pip install -r requirements.txt
python main.py                 :: LIVE on MT5 demo, always-on (add --dry-run to simulate)
python main.py --oneshot       :: one session then exit (debugging)
```

TWS paper must be logged in (port 7497, client IDs 21/22).

## MT5 execution (AMP demo)

Sends orders to the AMP MT5 terminal on the **same Windows machine** (the
MetaTrader5 package cannot reach a terminal on another box). Market data
(GEX, quotes) still comes from IBKR; only order flow moves to MT5.

VPS setup (one time):

```bat
cd C:\path\to\algo_es
.\venv\Scripts\pip install MetaTrader5
```

Then create a file named `.env` **next to main.py** with your demo login
(you create this — it is never committed anywhere):

```
MT5_PATH=C:\path\to\terminal64.exe
MT5_SERVER=AMPGlobalUSA-Demo
MT5_LOGIN=12345678
MT5_PASSWORD=your-password-here
MT5_SYMBOL=ESZ25
```

The terminal must have been logged in at least once so the symbol is
visible in Market Watch. If `MT5_SYMBOL` is wrong or missing, the algo
lists the ES/EP-like symbols it can see so you can pick the right one.

Run:

```bat
set ES_EXECUTOR=mt5
.\venv\Scripts\python main.py            :: LIVE: transmits to MT5 demo (add --dry-run to simulate)
```

Notes:

- Entries are **market** orders with SL+TP attached in the same request
  (MT5 holds them server-side — no OCA group to manage). The IBKR backend's
  fade limit-at-the-wall becomes a market entry on MT5.
- 1 contract = 1.0 MT5 volume. Commission is booked at $5.00 round-turn per
  contract (AMP demo ES estimate — reconcile against your statement).
- default is LIVE transmission to the MT5 demo account; `--dry-run` simulates without sending.
- The password is read from the environment only and is never logged.

## Config (env, `ES_` prefix)

| var | default | meaning |
|---|---|---|
| `ES_EXECUTOR` | ibkr | order backend: `ibkr` (TWS paper) or `mt5` (AMP demo) |
| `ES_GEX_EVAL_SEC` | 300 | wall-clock: walls re-evaluated every 5 min |
| `ES_FADE_MIN_CONFIDENCE` | 0.5 | fade needs wall confidence ≥ this |
| `ES_BREAKOUT_MIN_CONFIDENCE` | 0.35 | breakout arming needs ≥ this |
| `ES_OI_SWEEP_MIN` | 60 | full-chain OI+gamma sweep cadence |
| `ES_FLATTEN_BEFORE_HALT` | true | flatten at 16:55 ET before the 17:00 halt |
| `ES_RISK_PER_TRADE` | 200 | $ risk/trade → contracts = floor(200/(stop×50)) |
| `ES_MAX_TRADES_PER_DAY` | 4 | |
| `ES_DAILY_STOP_PCT` | 2 | −2% NetLiq kill switch |
| `ES_SLEEVE_C` | 1 | overnight range-fade sleeve on/off |
| `ES_SLEEVE_D` | 1 | overnight sweep+reclaim sleeve on/off |
| `ES_PA_RANGE_MIN` / `ES_PA_RANGE_MAX` | 8 / 30 | C fades only when ON range width in [min,max] pts |
| `ES_PA_STOP` | 4 | C stop: this many pts beyond the extreme |
| `ES_SWEEP_MIN` | 2 | D sweep: extreme exceeded by ≥ this (pts) |
| `ES_SWEEP_RECLAIM_MIN` | 15 | D reclaim window (minutes) |
| `ES_MT5_BAR_TZ` | UTC | ZoneInfo for MT5 bar timestamps (verify vs 18:00 ET reset) |

## Overnight PA sleeves (C + D)

Overnight GEX walls are frozen/stale, so these two sleeves trade **pure
price action** off MT5-built M1 bars (`mt5_data.py`: `copy_rates_from_pos`,
equivalent to aggregating MT5's tick stream — no tick storage). They never
read the bridge file for OHLC (only for the session flag + walls). Fail-soft:
if the MT5 feed can't connect, the PA sleeves go dormant (`PA_DATA_DOWN`)
and everything else keeps running.

- **Sleeve C — overnight range fade** (mean reversion; Globex is rotational
  ~70%): tracks ON high/low since 18:00 ET. Fades only when the range is
  8–30 pts wide (too tight = chop, too wide = already trended). Touch within
  1pt of ON high → short; within 1pt of ON low → long. Stop 4pts beyond the
  extreme; target = 70% retracement toward the opposite extreme. Max 2 fades
  per side per night.
- **Sleeve D — liquidity sweep + reclaim** (stop-hunts in thin Globex):
  price exceeds ON high / undercuts ON low by ≥2pts (the sweep) AND a bar
  closes back inside the pre-sweep range within 15 min → reversal (long
  after downside sweep, short after upside). Stop 3pts beyond the sweep
  extreme; target = opposite ON extreme. Max 1 per side per night.
- **Shared:** active only 18:00–09:00 ET; hard blackout 09:00–09:30 (NY
  handoff chop). D overrides C — any sweep on a side blocks further C fades
  at that extreme for the night. One position at a time; shared 4-trades/day
  budget; overnight size halved (`max(1, size//2)`). Sleeve name logged on
  every signal (`sleeve: C/D` in `CANDIDATE`).

## Files

- `main.py` — always-on supervisor + Globex session loop
- `config.py` — knobs, Globex session/halt math, PA session helpers
- `fop.py` — FOP chain discovery, hourly OI+gamma sweeps, ES streaming, M1 closes
- `mt5_data.py` — **MT5 M1-bar + quote feed** for the PA sleeves (fail-soft)
- `gex.py` — **StableWall estimator** (5-min wall clock, TWAP15 gamma,
  strike smoothing, hysteresis, zones, confidence)
- `strategy.py` — fade/breakout candidates, zone-edge `WallBreakState`,
  futures `Position` (TP1/BE/stop dry-run flow), **`OvernightPA`** (sleeves C/D)
- `orders.py` — entries + native OCA stop/target brackets, TP1→breakeven rebracket
- `risk.py` — Globex-day risk ($200/trade sizing, 4/day, −2% kill)
- `ibkr_conn.py`, `pacer.py` — copied verbatim from `~/workspace/algo/`

## Log events

`WALLS` (every 5-min eval: wall, zone, confidence, tenure, flip, regime),
`CANDIDATE`, `ENTRY`, `TP1`, `NATIVE_FILL`, `CLOSED`, `WB_ARM`, `RISK_BLOCK`,
`SESSION_START/END`, `DAY_END`, `SLEEP`, `SHUTDOWN`, `MT5_DATA_CONNECTED`,
`PA_DATA_DOWN`. JSONL, one file per Globex session day. PA signals log as
`PA-C fade …` / `PA-D sweep …` / `PA-D reclaim …` / `PA-D reversal …`.

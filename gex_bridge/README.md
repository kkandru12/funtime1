# gex_bridge — the single IBKR streaming connection

**Data only. This process NEVER places orders.** It is the account's one
streaming connection (clientId=1). Both algos are consumers of
`../shared/levels.json`; they hold zero market-data lines.

## Line budget (hard cap 100, target ≤75)

| Lines | What |
|---|---|
| 1 | SPX index (spot) |
| 1 | ES front futures ContFuture (basis = ES − SPX) |
| 42 | SPXW 0DTE active window: spot ±50, 5-pt strikes, both rights |
| 20 | SPXW 0DTE crush band: 55–95 pts OTM, 10-pt steps, both sides/rights |
| **64** | **sustained** |

OI is frozen intraday (verified): one paced morning snapshot (subscribe →
dwell → cancel), never held lines. GEX = frozen OI × live gamma via the
StableWall estimator (5-min wall clock, TWAP15 gamma, strike smoothing,
1.25×/3-eval hysteresis, zones not lines, confidence gating).

AllLast flow was tiebreak-only in v3 and is NOT streamed by the bridge.

## Sessions (ET)

- **NY 09:30–16:05** — full streaming, fresh 5-min wall evals, 0DTE crush +
  wall-break candidate screens, publish every 5s.
- **Overnight** — chain streaming cancelled (SPX + ES kept). Walls frozen:
  `stale:true`, `wall_ts_utc`, `wall_age_min`, confidence decayed ×
  max(0.3, 1 − age_min/240). Publish every 15s.
- **Weekend** (Sat, Fri ≥17:00, Sun <17:55) — sleep until Sun 17:55 ET.

## levels.json contract (atomic tmp+rename publish)

```jsonc
{
  "ts_utc": "...", "session": "ny|overnight",
  "spx": 6500.1, "es": 6520.5, "basis": 20.4, "basis_ts_utc": "...",
  "walls": {
    "call": {"strike": 6550, "zone_lo": 6545, "zone_hi": 6555,
             "confidence": 0.72, "tenure_min": 45.0,
             "gex_b": null, "dominance": 3.1},
    "put":  {...}
  },
  "es_walls": {"call": {...ES-converted, 0.25 grid...}, "put": {...}},
  "flip": 6480, "regime": "+", "net_gex_b": 1.23, "magnets": [6600],
  "wall_ts_utc": "...", "stale": false, "wall_age_min": 0.0,
  "candidates": [
    {"key": "6560C", "strike": 6560, "right": "call",
     "ask": 0.35, "bid": 0.30, "delta": 0.05, "gamma": 0.004,
     "iv": 0.12, "score": 0.0114, "ask_z": -1.4, "window": "B",
     "otm": 10.0, "spot": 6550.0, "wall": 6600, "trigger": "crush"}
  ],
  "chain_frame": {"6560C": {"bid":0.30,"ask":0.35,"delta":0.05,
                            "iv":0.12,"gamma":0.004}, ...},
  "oi_ts_utc": "...",
  "regime_info": {"mode": "observe", "score": null, "detail": null}
}
```

`shared/contracts.json`: all discovered 0DTE contracts with full descriptors
(symbol, exchange, tradingClass, lastTradeDateOrContractMonth, strike, right,
multiplier, currency) — consumers build + qualify order contracts from this.

## Fail-safes

- Bridge down / `levels.json` missing or age >60s in NY → consumers enter
  NOTHING (`STALE_LEVELS`), keep managing open positions.
- IBKR disconnect → reconnect loop with backoff (same clientId=1).
- Any loop exception → logged, 10s backoff, loop continues (always-on).

## Run

```powershell
cd gex_bridge
..\.venv\Scripts\Activate.ps1
$env:BRIDGE_IB_PORT = "7497"   # TWS paper; Gateway paper = 4002
python main.py
```

Start the bridge FIRST, then `algo/` and `algo_es/`. All three folders must
be siblings so `../shared` resolves.

# Bridge deploy guide (VPS 38.240.47.79, Administrator)

Two zips, same `gex_bridge/` inside both — extract each once:

- `algo_v3_bridge.zip` → `algo/` + `gex_bridge/` + `shared/`
- `algo_es_bridge.zip` → `algo_es/` + `gex_bridge/` + `shared/`

All three folders must be **siblings** (so `../shared` resolves).

## 1. Stop the old processes

The currently-running `algo/main.py` (pre-bridge, DRY-RUN) must be stopped —
it streams the chain itself and would double the line budget. Ctrl+C it
(it flattens first, it's dry-run so nothing to flatten).

## 2. Extract (PowerShell)

```powershell
# pick a home, e.g. C:\algos
Expand-Archive algo_v3_bridge.zip -DestinationPath C:\algos
Expand-Archive algo_es_bridge.zip -DestinationPath C:\algos
# -> C:\algos\algo, C:\algos\algo_es, C:\algos\gex_bridge, C:\algos\shared
```

## 3. Python envs

```powershell
# bridge + 0DTE algo need ib_insync (real one, not the stub)
cd C:\algos\gex_bridge;  pip install -r requirements.txt
cd C:\algos\algo;        pip install -r requirements.txt
# ES algo: MT5 only (zero IBKR now — ib_insync NOT needed)
cd C:\algos\algo_es;     pip install -r requirements.txt   # MetaTrader5 + numpy + tzdata
```

## 4. MT5 `.env` (algo_es only, on the VPS — never leaves the machine)

```powershell
# C:\algos\algo_es\.env
MT5_PATH=C:\Program Files\AMP Trading MetaTrader 5\terminal64.exe
MT5_SERVER=AMPGlobal-Demo       # your demo server
MT5_LOGIN=12345678              # numeric
MT5_PASSWORD=********
MT5_SYMBOL=                     # blank = auto-detect ES
```

## 5. Start order — BRIDGE FIRST, then the algos

```powershell
# window 1: the single streaming connection (clientId=1, data only)
cd C:\algos\gex_bridge;  python main.py
# wait for: "contracts.json published" + "NY session live: spot=... lines=64"

# window 2: 0DTE options algo (order-only, clientId=7, paper-gated)
cd C:\algos\algo;        python main.py            # dry-run
# cd C:\algos\algo;      python main.py --live     # transmit to DU* paper

# window 3: ES futures algo (MT5 demo)
cd C:\algos\algo_es;     $env:ES_EXECUTOR="mt5"; python main.py            # dry-run
# cd C:\algos\algo_es;   $env:ES_EXECUTOR="mt5"; python main.py --live     # MT5 demo transmit
```

`--live` on the 0DTE algo = IBKR **paper** account only (DU* hard gate, no
override). `--live` on the ES algo = MT5 **demo** only (no live-money path).

## 6. Verify

1. `C:\algos\shared\levels.json` appears and refreshes every ~5s in NY
   (`ts_utc` keeps moving; `session: "ny"`; `candidates` during windows).
2. `C:\algos\shared\contracts.json` published once per session.
3. Bridge log: `WALLS cw=... cc=...` every 5 min (StableWall clock).
4. 0DTE algo log: `LEVELS_OK`, `CONTRACTS`, then `RUN (consumer mode)`.
   If the bridge is down you see `STALE_LEVELS` and zero entries — by design.
5. ES algo log: `LEVELS_STATUS`, MT5 connect, `RUN (consumer mode)`.

## Line budget (the whole point)

| Process | Lines |
|---|---|
| `gex_bridge` (clientId=1) | 64 sustained: 1 SPX + 1 ES futures + 42 active window + 20 crush band (hard cap 100) |
| `algo` (clientId=7) | **0 streaming** — order-only connection |
| `algo_es` | **0 IBKR** — MT5 only |

OI snapshot + wing sweeps are paced one-shot requests (subscribe → dwell →
cancel), never held lines. AllLast flow (tiebreak-only) is not streamed.

## Sessions

- Bridge NY 09:30–16:05 ET: streaming + walls + candidate screens → `levels.json` every 5s.
- Bridge overnight: chain cancelled (SPX + ES kept), walls frozen
  (`stale:true`, confidence decayed × max(0.3, 1−age/240)) → every 15s.
- Bridge weekend: sleeps Fri 17:00 → Sun 17:55 ET.
- 0DTE algo: NY only; waits for bridge NY levels; 15:55 flat; always-on.
- ES algo: Globex Sun 18:00 → Fri 17:00; NY = fade+breakout, overnight =
  breakouts (half size, +0.15 conf) + PA sleeves C/D; 16:55 pre-halt flatten.

## Smoke tests

`gex_bridge/smoke_bridge.py` — 49 checks, all passing 2026-10-01:
atomic publish/consume, stale fail-safe (60s block), overnight confidence
decay, session detection, ES=SPX+basis, 0DTE screen ranking, zone wall-break
(arm + 2-close break), StableWall walls, BridgeGex adapter, PA sleeves C/D.
Needs `ib_insync` (real on the VPS; a stub sufficed for offline testing).

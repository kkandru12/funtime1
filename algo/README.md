# 0DTE SPXW 10X-OTM Algo (v3)

Buys crushed far-OTM SPXW 0DTE puts/calls in two measured windows —
**11:00–12:30 ET (40–55 pts OTM)** and **15:30–15:58 ET (8–12 pts OTM)** —
ask $0.20–$0.50, |delta| < 0.15, ask z-score in [−2.0, −1.0] vs trailing
30-min. Target: minimum 10x per trade ($200 risk → $2,000), stretch 50x.

Exits (v3 tiered, default): **1/3 of the position at 5x, 1/3 at 10x** (resting
native limits placed at fill), runner on a **30% giveback trail**. No per-trade
stop by default (measured: removing it added $37,100 over 5 backtest days);
the −2% NetLiq daily kill switch is the backstop. Flat everything 15:55 ET.

v3 (2026-10-01) adds three independent, config-toggled modules on the v2 merged
core: **tiered exits** (`CRUSH_TIERED_EXITS`, default on), a **wall-break
sleeve** (`CRUSH_WALLBREAK_ENABLED`, default on — dominant-wall break entries
inside the same A/B windows, outrights), and a **regime/day filter**
(`CRUSH_REGIME_MODE=observe`, default — logs an energy score, does not trade
on it; uncalibrated). Full evidence and open calibration work in `BUILD_NOTES.md`.

**ALWAYS-ON (default):** the process never exits on its own. It runs the
session (connect → trade → 15:55 flatten → 16:05 disconnect), then sleeps —
interruptibly, so Ctrl+C always lands promptly — until the next weekday
09:30 ET and reconnects fresh. Weekends, holidays and post-close runs just
sleep to the next session. `--oneshot` runs a single session/day-check then
exits (for debugging).

**CONSUMER MODE (bridge architecture).** The bridge (`../gex_bridge`) is the
account's single IBKR streaming connection (clientId=1). This algo holds
ZERO market-data lines: it connects order-only (clientId=7, paper-gated) and
reads chain quotes, StableWall walls/zones, candidates and regime from
`../shared/levels.json` (atomic publish, 5s cadence in NY). The 0DTE crush
and wall-break screens run in the bridge; this process consumes the ranked
candidates, applies its own risk manager, and manages exits.
**Fail-safe:** no NEW entries when levels.json is missing or older than 60s
(`STALE_LEVELS`); open positions keep being managed.
Start the bridge FIRST, then this algo. All three folders must be siblings
so `../shared` resolves.

**PAPER ONLY.** The algo hard-fails on any non-paper IBKR account (no override).

## 1. IB Gateway setup (on the VPS)

The algo targets **IB Gateway** (headless, lighter than TWS). TWS stays as manual backup.

1. Install IB Gateway, log in with the **paper** account credentials.
2. Configure → Settings → API → Settings:
   - ✅ Enable ActiveX and Socket Clients
   - ✅ Allow connections from localhost only (uncheck "Read-Only API" — the algo
     must place orders (LIVE by default; `--dry-run` only simulates)
   - Socket port: **4002** (Gateway paper). TWS paper alternative: **7497**.
   - Add trusted IP `127.0.0.1` if the API panel requires it.
3. Keep Gateway running. The algo connects to `127.0.0.1:4002`.

## 2. Deploy to the VPS

From this machine (where `~/workspace/algo` lives):

```bash
# copy to the VPS (3 commands)
rsync -avz --exclude venv --exclude logs ~/workspace/algo/ user@vps:~/algo/
ssh user@vps "cd ~/algo && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
```

On the VPS, configure via env (all optional; defaults target Gateway paper):

```bash
export CRUSH_IB_HOST=127.0.0.1
export CRUSH_IB_PORT=4002          # 7497 for TWS paper
export CRUSH_IB_CLIENT_ID=7       # must not collide with other API clients
```

## 3. Run

```powershell
cd C:\Users\Administrator\Downloads\algo\algo
.\venv\Scripts\python main.py            # DRY-RUN (default), ALWAYS-ON:
                                         # trades the session, sleeps to next
                                         # weekday 09:30 ET, reconnects fresh.
                                         # Places NO orders.
.\venv\Scripts\python main.py              # LIVE: real orders on the PAPER account
.\venv\Scripts\python main.py --dry-run  # simulate only, no orders
.\venv\Scripts\python main.py --oneshot  # one session/day-check then exit
                                         # (debugging; the old behavior)
```

Add `--dry-run` to simulate without transmitting. Every decision is JSON-logged to
`logs\algo_YYYYMMDD.jsonl` (a new file per calendar day) for audit.

Ctrl+C / SIGTERM always exits promptly: the current session flattens first,
then the process stops instead of sleeping.

## 4. Autostart (Windows Task Scheduler, on the VPS)

Always-on mode means **no daily schedule is needed** — start it once and it
runs every trading day by itself. Task Scheduler is now only a watchdog:
restart the process after a VPS reboot.

```powershell
# one-time: run at system startup (or at logon of the Administrator account)
$action = New-ScheduledTaskAction -Execute "C:\Users\Administrator\Downloads\algo\algo\venv\Scripts\python.exe" `
    -Argument "main.py" -WorkingDirectory "C:\Users\Administrator\Downloads\algo\algo"
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "CrushAlgo" -Action $action -Trigger $trigger `
    -Settings $settings -User "Administrator" -RunLevel Highest
```

Notes:
- TWS/Gateway (paper, API enabled, port 7497) must be running and logged in —
  start it via its own scheduled task / autostart before the algo.
- If the algo ever exits on its own (it shouldn't — SystemExit-class failures
  like a paper-gate refusal sleep to the next session instead), the 5-minute
  restart interval above brings it back.
- To stop it for the day: end the `CrushAlgo` task, or Ctrl+C in its console —
  the open position is flattened before exit.

## 5. What it does each day

| Time (ET) | Action |
|---|---|
| start | connect (paper-gated) → discover 0DTE chain → one full-chain OI snapshot |
| start+ | stream SPX + active window (±50, 5-pt) + crush band (55–95 OTM) ≈ 63 lines |
| every 15 min | re-center window on spot; wing sweep (snapshots) for GEX completeness |
| 11:00–12:30 | crush entries: 40–55 pts OTM, ask $0.20–$0.50, delta<0.15, ask z in [−2.0,−1.0], IV flat |
| 15:30–15:58 | crush entries: 8–12 pts OTM (same filters); wall-break sleeve armed on dominant walls |
| on entry | limit at ask (+$0.05 chase once, give up at 60s); size = $200 // ask |
| exits | 1/3 at 5x · 1/3 at 10x (resting native) · runner on 30% giveback trail · 15:55 flat |
| risk | max 2 trades/day, 1 position, −2% daily kill switch (flattens) |
| 16:05 | session ends → disconnect → sleep until next weekday 09:30 ET (always-on) |

See [BUILD_NOTES.md](sandbox://workspace/algo/BUILD_NOTES.md) for the line-budget
design, parameter provenance, and assumptions.

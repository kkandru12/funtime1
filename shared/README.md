# shared/ — the bridge's publish directory

`gex_bridge` (the account's single IBKR streaming connection) atomically
publishes here:

- `levels.json` — chain quotes/Greeks frame, StableWall walls+zones
  (SPX and ES-converted), flip, regime, magnets, 0DTE candidates,
  session flag. NY: every 5s. Overnight: every 15s (frozen walls,
  `stale:true`, confidence decayed).
- `contracts.json` — all discovered 0DTE SPXW contract descriptors
  (published once per NY session at discovery).

Consumers (`algo/`, `algo_es/`) are read-only here. All three folders must
be siblings so `../shared` resolves. Start the bridge FIRST.

#!/usr/bin/env python3
"""FunTime dashboard -- READ-ONLY web page (v1.08 DASH, 2026-10-02).

v1.09 2026-10-02 [LOCALPLOTLY] serves the bundled dashboard/plotly.min.js
    (plotly.js 4.1.1, MIT) at /plotly.min.js, so the chart works without
    internet access to the CDN ("ReferenceError: Plotly is not defined").

What it is
  One page at http://<DASH_HOST>:<DASH_PORT>/ (default 127.0.0.1:9100) that
  refreshes every 5 s: the SPX GEX histogram (call / put / net $B per strike)
  with spot, walls + zones, flip and magnets; ES (MT5) and 0DTE (IBKR)
  positions, P&L, candidates and today's trades; component health.

What it is NOT (KK: "make sure it disturbs nothing")
  - no IBKR or MT5 connection, no client ids, no orders, no buttons
  - only HTTP GET; every other method -> 405
  - it only READS files the bots already write:
      shared/levels.json, shared/gex_profile.json          (gex_bridge)
      logs/status_es.json,  logs/algo_es_YYYYMMDD.jsonl     (algo_es)
      logs/status_0dte.json, logs/algo_YYYYMMDD.jsonl       (algo)
  - no URL ever maps to a file path (no traversal); it writes nothing
  - if it crashes, nothing else notices (run_all restarts it)

Settings (.env): DASH_HOST (bind address; set your Tailscale IP, e.g.
100.123.238.119, to reach it from your PC), DASH_PORT (9100).
"""
import json, os, sys, time
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ET = ZoneInfo("America/New_York")


def _load_dotenv():
    for path in (os.path.join(HERE, ".env"), os.path.join(ROOT, ".env")):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except OSError:
            pass


_load_dotenv()
HOST = os.getenv("DASH_HOST", "127.0.0.1")
PORT = int(os.getenv("DASH_PORT", "9100"))
SHARED = os.getenv("SHARED_DIR") or os.path.join(ROOT, "shared")
LOGS = os.getenv("DASH_LOG_DIR") or os.path.join(ROOT, "logs")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        return doc, round(time.time() - os.path.getmtime(path), 1)
    except Exception:  # noqa: BLE001
        return None, None


def _events(path, want, limit=200):
    """Last JSON events of the given types from a jsonl log (tail only)."""
    out = []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 2_000_000))
            lines = f.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return out
    for ln in lines:
        if not ln.startswith("{"):
            continue
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if e.get("event") in want:
            out.append(e)
    return out[-limit:]


def state():
    day = datetime.now(ET).strftime("%Y%m%d")
    lv, lv_age = _read_json(os.path.join(SHARED, "levels.json"))
    if lv:
        lv = {k: v for k, v in lv.items() if k != "chain_frame"}   # big; not needed
    prof, prof_age = _read_json(os.path.join(SHARED, "gex_profile.json"))
    es, es_age = _read_json(os.path.join(LOGS, "status_es.json"))
    od, od_age = _read_json(os.path.join(LOGS, "status_0dte.json"))
    es_ev = _events(os.path.join(LOGS, f"algo_es_{day}.jsonl"),
                    {"ENTER", "SIM_ENTER", "FILL", "SIM_FILL", "CLOSED", "NO_FILL",
                     "ORDER_REJECT", "GLOBEX_SIGNAL", "GLOBEX_REJECT", "MT5_RECONNECT",
                     "MT5_RECONNECT_FAIL", "MT5_DATA_DOWN", "MT5_EXEC_DOWN", "RISK_BLOCK",
                     "NATIVE_FILL", "CLOSE_SKIPPED", "EXIT_ORDER"})
    od_ev = _events(os.path.join(LOGS, f"algo_{day}.jsonl"),
                    {"CANDIDATE", "ENTER", "SIM_ENTER", "FILL", "SIM_FILL", "TIER_FILL",
                     "TRAIL_ARM", "CLOSED", "NO_FILL", "ENTER_UNFILLED", "SPREAD_NONE",
                     "FATAL", "CARRY", "CARRY_RESUMED", "CARRY_GONE", "EXITS_FROZEN",
                     "RISK_BLOCK", "SESSION_RETRY"})
    return {"now": datetime.now(ET).isoformat(timespec="seconds"),
            "levels": lv, "levels_age_s": lv_age,
            "profile": prof, "profile_age_s": prof_age,
            "es": es, "es_age_s": es_age, "odte": od, "odte_age_s": od_age,
            "es_events": es_ev[-60:], "odte_events": od_ev[-60:]}


PAGE = os.path.join(HERE, "index.html")
PLOTLY = os.path.join(HERE, "plotly.min.js")      # [v1.09] bundled chart library


class H(BaseHTTPRequestHandler):
    server_version = "FunTimeDash/1.08"

    def _send(self, code, body: bytes, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                with open(PAGE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self._send(500, b"index.html missing", "text/plain")
        if path == "/plotly.min.js":                # fixed file, never a URL path
            try:
                with open(PLOTLY, "rb") as f:
                    return self._send(200, f.read(), "application/javascript")
            except OSError:
                return self._send(404, b"plotly.min.js missing", "text/plain")
        if path == "/api/state":
            body = json.dumps(state(), default=str).encode()
            return self._send(200, body, "application/json")
        return self._send(404, b"not found", "text/plain")

    def _no(self):
        self._send(405, b"read-only dashboard", "text/plain")

    do_POST = do_PUT = do_DELETE = do_PATCH = _no

    def log_message(self, *a):   # quiet: no per-request console spam
        pass


def main():
    srv = ThreadingHTTPServer((HOST, PORT), H)
    print(f"dashboard (read-only) on http://{HOST}:{PORT}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

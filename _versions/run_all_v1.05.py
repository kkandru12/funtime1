#!/usr/bin/env python3
"""Single-process supervisor: runs gex_bridge + algo + algo_es together.

Usage:
    python run_all.py [--dry-run] [--live]
    start.bat  (dry-run by default)

- Starts all three as subprocesses in ONE console window.
- Prefixes every log line with the component name.
- Restarts any component that exits, FOREVER (v1.05 RESILIENT): backoff
  5s -> 10s -> ... -> 60s; the backoff resets after 10 min of healthy running.
  (Before v1.05 it gave up after 5 crashes -- an IBKR/MT5 outage longer than
  a few minutes left that component dead until someone restarted it.)
- Ctrl+C stops everything cleanly.
- --live passes --live to both algos (paper/demo orders). Default is dry-run.
"""
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, "venv", "Scripts", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable  # fallback: system python

DRY = "--live" not in sys.argv

COMPONENTS = [
    ("bridge", ["gex_bridge/main.py", []]),
    ("algo", ["algo/main.py", ["--dry-run"] if DRY else ["--live"]]),
    ("algo_es", ["algo_es/main.py", ["--dry-run"] if DRY else ["--live"]]),
]

BACKOFF_START, BACKOFF_MAX, HEALTHY_SEC = 5, 60, 600


def stream_output(name, proc):
    for line in proc.stdout:
        print(f"[{name}] {line}", end="", flush=True)


def run_component(name, script, args):
    restarts = 0
    backoff = BACKOFF_START
    while True:
        print(f"[supervisor] starting {name} (attempt {restarts + 1})", flush=True)
        started = time.time()
        proc = subprocess.Popen(
            [PY, os.path.join(HERE, script)] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=HERE,
        )
        t = threading.Thread(target=stream_output, args=(name, proc), daemon=True)
        t.start()
        proc.wait()
        restarts += 1
        if time.time() - started >= HEALTHY_SEC:
            backoff = BACKOFF_START          # it ran fine for a while: reset
        print(f"[supervisor] {name} exited (code {proc.returncode}); "
              f"restarting in {backoff}s", flush=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_MAX)


def main():
    mode = "DRY-RUN (no orders)" if DRY else "LIVE (paper/demo orders)"
    print(f"[supervisor] starting all components — {mode}", flush=True)
    print("[supervisor] Ctrl+C to stop everything", flush=True)
    threads = []
    for name, (script, args) in COMPONENTS:
        t = threading.Thread(target=run_component, args=(name, script, args), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(2)  # stagger starts: bridge first
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[supervisor] stopping all components...", flush=True)
        # daemon threads die with the process; subprocesses get killed below
        os._exit(0)


if __name__ == "__main__":
    main()

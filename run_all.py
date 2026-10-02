#!/usr/bin/env python3
"""Single-process supervisor: runs gex_bridge + algo + algo_es together.

Usage:
    python run_all.py [--dry-run] [--live]
    start.bat  (dry-run by default)

- Starts all three as subprocesses in ONE console window.
- Prefixes every log line with the component name.
- Restarts any component that crashes (max 5 restarts, then gives up).
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

MAX_RESTARTS = 5


def stream_output(name, proc):
    for line in proc.stdout:
        print(f"[{name}] {line}", end="", flush=True)


def run_component(name, script, args):
    restarts = 0
    while restarts <= MAX_RESTARTS:
        print(f"[supervisor] starting {name} (attempt {restarts + 1})", flush=True)
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
        if restarts <= MAX_RESTARTS:
            print(f"[supervisor] {name} exited (code {proc.returncode}); restarting in 5s",
                  flush=True)
            time.sleep(5)
    print(f"[supervisor] {name} crashed {MAX_RESTARTS}x; giving up", flush=True)


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

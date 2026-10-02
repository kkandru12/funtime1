"""v1.01 2026-10-02 [WINREPLACE] On Windows os.replace fails with WinError 5
    (Access is denied) while a reader (algo / algo_es / dashboard) has
    levels.json open -- seen live 09:45:25. Retry up to 20 x 50 ms; if still
    locked, skip this publish (the next one 5 s later replaces it) instead of
    raising into the bridge loop.

Atomic JSON publish: write tmp, fsync, os.replace.

Readers must never see a half-written levels.json.
"""
import json
import os
import tempfile
import time


def publish(path: str, payload: dict):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".levels-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        for i in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if i == 19:
                    return False          # reader still holds it: skip this one
                time.sleep(0.05)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return True

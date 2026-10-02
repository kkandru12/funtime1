"""Token-bucket API pacer.

IBKR throttles aggressively on bursty request patterns (the documented one is
60 historical requests / 10 min; market-data snapshots are policed by the
100-line cap plus an unpublished burst limit). Every snapshot sweep,
re-centering batch, and OI scan in this algo goes through this pacer so we
never trip a pacing violation.
"""
import asyncio
import time


class Pacer:
    def __init__(self, rate_per_sec: float, max_concurrent: int = 8):
        self.rate = rate_per_sec
        self.tokens = rate_per_sec  # start full: allow a small initial burst
        self.max_tokens = rate_per_sec
        self.sem = asyncio.Semaphore(max_concurrent)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self.sem:
            async with self._lock:
                now = time.monotonic()
                self.tokens = min(self.max_tokens,
                                  self.tokens + (now - self._last) * self.rate)
                self._last = now
                if self.tokens < 1.0:
                    wait = (1.0 - self.tokens) / self.rate
                    await asyncio.sleep(wait)
                    self.tokens = 0.0
                    self._last = time.monotonic()
                else:
                    self.tokens -= 1.0

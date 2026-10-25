"""Short-window traffic rates; audio payload is never retained."""
from collections import deque
import threading
import time


class TrafficMeter:
    def __init__(self, clock=time.monotonic, window=2.0):
        self.clock, self.window = clock, window
        self.started = clock()
        self.samples = deque()
        self.lock = threading.Lock()

    def add(self, size):
        now = self.clock()
        with self.lock:
            self.samples.append((now, max(0, size)))
            self._trim(now)

    def _trim(self, now):
        while self.samples and self.samples[0][0] < now - self.window:
            self.samples.popleft()

    def snapshot(self):
        now = self.clock()
        with self.lock:
            self._trim(now)
            elapsed = max(.25, min(self.window, now - self.started))
            return {"bitrate": round(sum(s[1] for s in self.samples) * 8 / elapsed),
                    "packets_per_second": round(len(self.samples) / elapsed, 1)}

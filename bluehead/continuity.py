"""Hysteresis for a damaged competing media stream; never changes user volume."""
import time


class ContinuityGuard:
    def __init__(self, rate=48000, clock=time.monotonic):
        self.rate, self.clock = rate, clock
        self.reset()

    def reset(self):
        self.protected = False
        self.started = None
        self.frames = self.packets = self.lost = self.errors = 0
        self.bad = self.good = 0

    def observe(self, frames, packets, lost, errors, competing):
        now = self.clock()
        if not competing:
            self.reset()
            return False
        if self.started is None:
            self.started = now
        self.frames += frames
        self.packets += packets
        self.lost += lost
        self.errors += errors
        elapsed = now - self.started
        if elapsed < .5:
            return self.protected
        supply = self.frames / (self.rate * elapsed)
        damaged = (self.errors >= 3 or self.lost >= 3) and supply < .8
        damaged |= self.packets >= 10 and supply < .55
        clean = supply >= .9 and self.lost == 0 and self.errors == 0
        self.bad = self.bad + 1 if damaged else 0
        self.good = self.good + 1 if clean else 0
        if self.bad >= 2:
            self.protected = True
        elif self.good >= 3:
            self.protected = False
        self.started = now
        self.frames = self.packets = self.lost = self.errors = 0
        return self.protected

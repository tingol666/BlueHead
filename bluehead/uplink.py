"""Native uplink mix, with session identity checked before every worker write."""
import ctypes as c
from pathlib import Path
import threading


class CallRouter:
    def __init__(self):
        self.owner = None
        self.muted = False
        self.microphone_gain = self.system_gain = 1.0
        self.device_gains = {}
        self.device_standard_gains = {}
        self.lock = threading.RLock()
        self.maximum_bytes, self.trim_bytes = 16000, 8000
        self.adaptive, self.rate = False, 16000
        self.api = c.CDLL(str(Path(__file__).parent / "assets/bluehead_audio.dll"))
        api = self.api
        api.bh_up_create.restype = c.c_void_p
        api.bh_up_free.argtypes = [c.c_void_p]
        api.bh_up_clear.argtypes = [c.c_void_p, c.c_int]
        api.bh_up_rate.argtypes = [c.c_void_p, c.c_uint]
        api.bh_up_adaptive.argtypes = [c.c_void_p, c.c_int]
        api.bh_up_adaptive.restype = c.c_int
        api.bh_up_value.argtypes = [c.c_void_p, c.c_int, c.c_int]
        api.bh_up_value.restype = c.c_uint64
        api.bh_up_gain.argtypes = [c.c_void_p, c.c_float, c.c_float, c.c_float, c.c_int]
        api.bh_up_write.argtypes = [c.c_void_p, c.c_int, c.c_void_p, c.c_uint]
        api.bh_up_read.argtypes = [c.c_void_p, c.c_void_p, c.c_uint]
        api.bh_up_size.argtypes = [c.c_void_p, c.c_int]
        api.bh_up_size.restype = c.c_uint
        self.handle = api.bh_up_create()
        if not self.handle:
            raise MemoryError("Native microphone mixer allocation failed")

    @property
    def buffer(self):
        # Compatibility for offline backlog checks. Production reads are native.
        with self.lock:
            return bytes(self.api.bh_up_size(self.handle, 0))

    @property
    def system_buffer(self):
        with self.lock:
            return bytes(self.api.bh_up_size(self.handle, 1))

    def configure(self, rate, *, adaptive=False):
        if adaptive and rate != 48000:
            raise ValueError("Adaptive capture requires 48 kHz PCM")
        with self.lock:
            if self.api.bh_up_rate(self.handle, rate) < 0:
                raise ValueError("Invalid capture rate")
            if self.api.bh_up_adaptive(self.handle, int(bool(adaptive))) < 0:
                raise ValueError("Invalid adaptive capture mode")
            self.adaptive, self.rate = bool(adaptive), rate
            self.maximum_bytes, self.trim_bytes = int(rate * 2 * .09), int(rate * 2 * .06)

    def select(self, key):
        with self.lock:
            self.owner = key
            self.api.bh_up_clear(self.handle, 2)

    def clear(self, key):
        with self.lock:
            if key == self.owner:
                self.api.bh_up_clear(self.handle, 0)

    def clear_system(self):
        with self.lock:
            self.api.bh_up_clear(self.handle, 1)

    def _write(self, key, data, bus):
        if not isinstance(data, bytes):
            raise TypeError("Capture PCM must be owned bytes")
        with self.lock:
            if key == self.owner:
                if self.adaptive and len(data) % 2:
                    raise ValueError("Adaptive capture requires complete S16 samples")
                self.api.bh_up_write(self.handle, bus, data, len(data))

    def write_microphone(self, key, data):
        self._write(key, data, 0)

    def write_system(self, key, data):
        self._write(key, data, 1)

    def read(self, key, length):
        if not 0 <= length <= 34560:
            raise ValueError("Unbounded microphone read")
        with self.lock:
            if key != self.owner:
                return bytes(length)
            if self.adaptive and length % 2:
                raise ValueError("Adaptive capture requires complete S16 samples")
            self.api.bh_up_gain(self.handle, self.microphone_gain, self.system_gain,
                                self.device_gains.get(key, 1.0) * self.device_standard_gains.get(key, 1.0), self.muted)
            output = c.create_string_buffer(length)
            if self.api.bh_up_read(self.handle, output, length) < 0:
                raise ValueError("Invalid microphone buffer")
            return output.raw

    def snapshot(self):
        with self.lock:
            output = {"adaptive": self.adaptive, "rate": self.rate, "buffer_ms": 30 if self.adaptive else 0,
                      "closed": not bool(self.handle), "buffer_reference": "render_center" if self.adaptive else "fifo"}
            for bus, name in enumerate(("microphone", "system")):
                values = {field: int(self.api.bh_up_value(self.handle, bus, index)) if self.handle else 0 for index, field in enumerate(
                    ("queued_frames", "dropped_frames", "drift_ppm", "underruns", "prefill_frames"))}
                if self.handle:
                    values["drift_ppm"] -= 1000
                output[name] = values
            return output

    clock_snapshot = snapshot

    def close(self):
        with self.lock:
            if getattr(self, "handle", None):
                self.api.bh_up_free(self.handle)
                self.handle = None

    def __del__(self):
        if hasattr(self, "lock"):
            self.close()

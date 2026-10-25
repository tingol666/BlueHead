"""Small owned-buffer interface to the native float32 ring mixer."""
import ctypes
from pathlib import Path

import numpy as np


class NativeMixer:
    def __init__(self):
        self.library = ctypes.CDLL(str(Path(__file__).parent / "assets/bluehead_audio.dll"))
        api = self.library
        api.bh_mix_create.restype = ctypes.c_void_p
        api.bh_mix_free.argtypes = [ctypes.c_void_p]
        api.bh_mix_add.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
        api.bh_mix_add.restype = ctypes.c_int
        for name in ("bh_mix_remove", "bh_mix_clear", "bh_mix_call"):
            getattr(api, name).argtypes = [ctypes.c_void_p, ctypes.c_int]
        api.bh_mix_gate.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        api.bh_mix_gain.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_float]
        api.bh_mix_write.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
        api.bh_mix_render.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_float]
        api.bh_mix_value.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        api.bh_mix_value.restype = ctypes.c_uint64
        self.handle = api.bh_mix_create()
        if not self.handle:
            raise MemoryError("PCM ring allocation failed")

    def add(self, target, maximum):
        index = self.library.bh_mix_add(self.handle, target, maximum)
        if index < 0:
            raise ValueError("PCM stream capacity exceeded")
        return NativeInput(self, index)

    def render_into(self, output, master):
        if (not isinstance(output, np.ndarray) or output.dtype != np.float32 or
                output.ndim != 2 or output.shape[1] != 2 or
                not output.flags.c_contiguous or not output.flags.writeable):
            raise ValueError("PCM output must be a writable contiguous stereo float32 array")
        result = self.library.bh_mix_render(self.handle, output.ctypes.data, len(output), master)
        if result < 0:
            raise ValueError("Invalid PCM output buffer")

    def close(self):
        if getattr(self, "handle", None):
            self.library.bh_mix_free(self.handle)
            self.handle = None

    def __del__(self):
        self.close()


class NativeInput:
    def __init__(self, mixer, index):
        self.mixer, self.index = mixer, index
        self.gain = 1.0

    @property
    def frames(self):
        return self.mixer.library.bh_mix_value(self.mixer.handle, self.index, 0)

    @property
    def dropped(self):
        return self.mixer.library.bh_mix_value(self.mixer.handle, self.index, 1)

    def write(self, samples):
        owned = np.ascontiguousarray(samples, dtype=np.float32)
        if owned.ndim != 2 or owned.shape[1] != 2:
            raise ValueError("PCM input must have two channels")
        self.mixer.library.bh_mix_write(self.mixer.handle, self.index, owned.ctypes.data, len(owned))

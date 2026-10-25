"""Experimental LDAC decoder isolated in a child process.

The MIT libldacdec implementation is not a Sony-certified decoder. Keep it opt-in.
"""
import ctypes
import multiprocessing as mp
from pathlib import Path

import numpy as np


class NativeLdac:
    def __init__(self):
        path = Path(__file__).resolve().parent / "assets" / "bluehead_ldac.dll"
        self.library = ctypes.CDLL(str(path))
        self.library.bh_ldac_create.restype = ctypes.c_void_p
        self.library.bh_ldac_free.argtypes = [ctypes.c_void_p]
        self.library.bh_ldac_decode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        self.handle = self.library.bh_ldac_create()
        if not self.handle:
            raise MemoryError("LDAC allocation failed")

    def decode(self, frame):
        output = np.zeros(512, dtype=np.float32)
        count = self.library.bh_ldac_decode(self.handle, frame, len(frame), output.ctypes.data, 512)
        if count <= 0:
            raise ValueError(f"LDAC rejected frame ({count})")
        return output[:count * 2].reshape(-1, 2)

    def close(self):
        if self.handle:
            self.library.bh_ldac_free(self.handle)
            self.handle = None


def _worker(pipe):
    decoder = NativeLdac()
    try:
        pipe.send_bytes(b"READY")
        while True:
            frame = pipe.recv_bytes(515)
            if not frame:
                break
            if frame == b"RESET":
                decoder.close()
                decoder = NativeLdac()
                pipe.send_bytes(b"READY")
                continue
            try:
                pipe.send_bytes(b"\x00" + decoder.decode(frame).tobytes())
            except ValueError:
                pipe.send_bytes(b"\x01")
                decoder.close()
                decoder = NativeLdac()
    except (EOFError, OSError):
        pass
    finally:
        decoder.close()
        pipe.close()


class LdacDecoder:
    def __init__(self, rate):
        self.rate = rate
        context = mp.get_context("spawn")
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=_worker, args=(child,), daemon=True)
        self.process.start()
        child.close()
        if not self.pipe.poll(8) or self.pipe.recv_bytes(16) != b"READY":
            self.close()
            raise RuntimeError("LDAC worker could not start")

    def reset(self):
        self.pipe.send_bytes(b"RESET")
        if not self.pipe.poll(.5) or self.pipe.recv_bytes(16) != b"READY":
            self.close()
            raise TimeoutError("LDAC decoder reset stalled")

    def decode(self, frame):
        if not 4 <= len(frame) <= 515:
            raise ValueError("Invalid LDAC frame length")
        self.pipe.send_bytes(frame)
        if not self.pipe.poll(0.5):
            self.close()
            raise TimeoutError("LDAC decoder stalled")
        data = self.pipe.recv_bytes(4097)
        if not data or data[0] != 0:
            raise ValueError("LDAC decoder rejected a damaged/unsupported frame")
        return np.frombuffer(data[1:], dtype=np.float32).reshape(-1, 2).copy()

    def close(self):
        if getattr(self, "process", None):
            try:
                self.pipe.send_bytes(b"")
            except (BrokenPipeError, EOFError, OSError):
                pass
            self.process.join(0.3)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(1)
            self.pipe.close()
            self.process.close()
            self.process = None

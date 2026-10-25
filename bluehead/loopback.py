"""Live computer audio excluding this worker process tree; no recordings."""
import ctypes
import os
from pathlib import Path
import sys
import threading

import numpy as np
from .realtime import NativeCapture

_retired = []  # Keep handles/DLLs owned if an OS Stop has not returned yet.


def supported():
    return sys.platform == "win32" and sys.getwindowsversion().build >= 20348


class ProcessCapture:
    def __init__(self, excluded_pid=None):
        if not supported():
            raise OSError("电脑声音混合需要 Windows 11 或 Windows build 20348 及以上")
        self.handle = None
        self.lock = threading.Lock()
        self.api = ctypes.CDLL(str(Path(__file__).parent / "assets/bluehead_loopback.dll"))
        self.api.bh_loop_open.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        self.api.bh_loop_open.restype = ctypes.c_void_p
        self.api.bh_loop_read.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        self.api.bh_loop_read.restype = ctypes.c_int
        self.api.bh_loop_error.argtypes = [ctypes.c_void_p]
        self.api.bh_loop_error.restype = ctypes.c_uint32
        self.api.bh_loop_close.argtypes = [ctypes.c_void_p]
        self.api.bh_loop_close.restype = ctypes.c_int
        for old in list(_retired):
            try:
                old.close()
            except OSError:
                pass
        error = ctypes.c_uint32()
        self.handle = self.api.bh_loop_open(excluded_pid or os.getpid(), ctypes.byref(error))
        if not self.handle:
            raise OSError(f"Windows 电脑声音采集不可用（0x{error.value:08X}），麦克风仍可使用")

    def read(self, output):
        if (not isinstance(output, np.ndarray) or output.dtype != np.float32 or output.ndim != 1 or
                not output.flags.c_contiguous or not output.flags.writeable or len(output) > 4800):
            raise ValueError("Loopback output must be contiguous mono float32, at most 4800 frames")
        with self.lock:
            if not self.handle:
                return 0
            count = self.api.bh_loop_read(self.handle, output.ctypes.data, len(output))
            if count < 0:
                error = self.api.bh_loop_error(self.handle)
                raise OSError(f"电脑声音采集已停止（0x{error:08X}）")
            return count

    def close(self):
        with self.lock:
            if self.handle:
                if not self.api.bh_loop_close(self.handle):
                    if self not in _retired:
                        _retired.append(self)
                    raise OSError("电脑声音采集仍在关闭中，将稍后重试")
                self.handle = None
            if self in _retired:
                _retired.remove(self)


class SystemCapture:
    """Separate bounded source; microphone and SCO clocks never wait for capture."""
    def __init__(self, route, key, rate, emit):
        self.route, self.key, self.emit = route, key, emit
        self.converter = NativeCapture(48000, 1, rate)
        self.raw = ProcessCapture()
        self.stop = threading.Event()
        self.frames = 0
        self.thread = threading.Thread(target=self.run, daemon=True, name="computer-audio")
        try:
            self.thread.start()
        except Exception:
            self.raw.close()
            self.converter.close()
            raise

    def run(self):
        buffer = np.empty(480, np.float32)
        output = np.empty(1024, np.int16)
        try:
            while not self.stop.is_set():
                count = self.raw.read(buffer)
                if count:
                    self.converter.push(buffer[:count])
                    while (produced := self.converter.read_into(output)):
                        self.route.write_system(self.key, output[:produced].tobytes())
                    self.frames += count
                else:
                    self.stop.wait(.005)
        except Exception as error:
            if not self.stop.is_set():
                self.emit("warning", message=f"电脑声音采集已停止，麦克风继续使用：{error}")
        finally:
            self.converter.close()
            self.route.close()
            try:
                self.raw.close()
            except OSError:
                pass

    def close(self):
        self.route.close()
        self.stop.set()
        self.thread.join(2)
        if self.thread.is_alive():
            raise OSError("电脑声音采集线程仍在关闭")
        self.raw.close()

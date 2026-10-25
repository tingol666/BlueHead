"""Prevent two BlueHead processes from driving the same HCI controller."""
import ctypes
import hashlib
import os


class AdapterLease:
    def __init__(self, transport):
        base = transport.split("+sco=", 1)[0].lower()
        self.name = "Local\\BlueHead-HCI-" + hashlib.sha256(base.encode()).hexdigest()[:24]
        self.handle = None
        self.kernel = None

    def acquire(self):
        if os.name != "nt":
            return
        from ctypes import wintypes
        kernel = self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        handle = kernel.CreateMutexW(None, False, self.name)
        error = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS, including another local process
            kernel.CloseHandle(handle)
            raise RuntimeError("这个蓝牙适配器已有 BlueHead 接收器在运行，请先停止另一个实例")
        self.handle = handle

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

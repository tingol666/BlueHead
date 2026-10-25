"""Balance COM initialization on every thread that controls WASAPI streams."""
from contextlib import contextmanager
import ctypes
import sys


@contextmanager
def audio_apartment():
    library = None
    owned = False
    if sys.platform == "win32":
        library = ctypes.WinDLL("ole32")
        library.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        library.CoInitializeEx.restype = ctypes.c_long
        result = library.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        owned = result in (0, 1)
        if not owned and result != -2147417850:  # Existing STA: RPC_E_CHANGED_MODE
            raise OSError(f"Audio COM initialization failed: 0x{result & 0xffffffff:08x}")
    try:
        yield
    finally:
        if owned:
            library.CoUninitialize()

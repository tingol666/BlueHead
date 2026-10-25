"""Opt-in local native exception metadata for radio-only crash diagnostics."""
import ctypes
import faulthandler
import os
from pathlib import Path
import sys

_resources = []


def enable(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stream = (directory / "native-errors.log").open("a", encoding="utf-8", buffering=1)
    os.dup2(stream.fileno(), 2)
    sys.stderr = stream
    faulthandler.enable(stream)
    dll = ctypes.CDLL(str(Path(__file__).parent / "assets/bluehead_trace.dll"))
    dll.install_trace.argtypes = [ctypes.c_wchar_p]
    dll.install_trace.restype = ctypes.c_int
    result = dll.install_trace(str((directory / "native-first-chance.log").resolve()))
    if result:
        raise OSError(result, "Native diagnostic initialization failed")
    _resources.extend((stream, dll))

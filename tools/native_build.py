"""Shared native source layout and compiler discovery; no build side effects."""
from pathlib import Path
import os
import shutil

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / 'native'
INCLUDE_DIRS = tuple(NATIVE / part for part in ('include', 'audio', 'runtime', 'transport'))


def includes(*extra):
    return [flag for path in (*INCLUDE_DIRS, *extra) for flag in ('-I', str(path))]


def compiler(cxx=False):
    name = 'g++' if cxx else 'gcc'
    configured = os.environ.get('CXX' if cxx else 'CC')
    candidates = (configured, shutil.which(name), f'D:/Code/MSYS2/ucrt64/bin/{name}.exe')
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise FileNotFoundError(f'{name} is required; configure {"CXX" if cxx else "CC"} or PATH')

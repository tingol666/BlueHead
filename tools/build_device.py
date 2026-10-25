"""Build pinned, WASAPI-only device I/O; no downloads or hardware operations."""
from pathlib import Path
import hashlib
import shutil
import subprocess

root = Path(__file__).resolve().parents[1]
vendor = root / "vendor/miniaudio"
expected = {
    "miniaudio.h": "ac7af4de748b7e26b777f37e01cee313a308a7296a3eb080e2906b320cc55c89",
    "LICENSE": "457f1b500e0adf6bc059edddfa78a2f62012e7c3bb43476c20e0bd23b25ba0eb",
}
for name, digest in expected.items():
    if hashlib.sha256((vendor / name).read_bytes()).hexdigest() != digest:
        raise RuntimeError("Modified pinned miniaudio 0.11.25 source: " + name)
compiler = shutil.which("gcc") or "D:/Code/MSYS2/ucrt64/bin/gcc.exe"
subprocess.run([
    compiler, "-shared", "-m64", "-std=c11", "-O3", "-Wall", "-Wextra", "-Werror",
    "-static-libgcc", "-Wl,--no-insert-timestamp", "-s", "-I", str(vendor),
    "-o", str(root / "bluehead/assets/bluehead_device.dll"), str(root / "native/audio_device.c"),
], check=True)

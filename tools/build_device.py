"""Build pinned, WASAPI-only device I/O; no downloads or hardware operations."""

from pathlib import Path
from native_build import compiler as find_compiler, includes
import hashlib
import argparse
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
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, default=root / "bluehead/assets/bluehead_device.dll")
options = parser.parse_args()
output = options.output.resolve()
output.parent.mkdir(parents=True, exist_ok=True)
compiler = find_compiler()
subprocess.run(
    [
        compiler,
        "-shared",
        "-m64",
        "-std=c11",
        "-O3",
        "-Wall",
        "-Wextra",
        "-Werror",
        *includes(),
        "-static-libgcc",
        "-Wl,--no-insert-timestamp",
        "-s",
        "-I",
        str(vendor),
        "-o",
        str(output),
        str(root / "native/platform/windows/audio_device.c"),
    ],
    check=True,
)

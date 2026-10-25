"""Build the allocation-free render core with MinGW-w64."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
gcc = shutil.which("gcc") or "D:/Code/MSYS2/ucrt64/bin/gcc.exe"
vendor = root / "vendor/speexdsp"
origin = json.loads((vendor / "origin.json").read_text("utf-8"))
for name, expected in origin["files_sha256"].items():
    if hashlib.sha256((vendor / name).read_bytes()).hexdigest() != expected:
        raise RuntimeError("Modified pinned SpeexDSP source: " + name)
with tempfile.TemporaryDirectory(prefix="bluehead-audio-") as build:
    objects = []
    for source in (root / "native/audio_mixer.c", root / "native/audio_realtime.c", vendor / "resample.c"):
        output = str(Path(build) / (source.stem + ".o"))
        flags = ["-Wno-sign-compare"] if source.parent == vendor else []
        subprocess.run([gcc, "-c", "-std=c11", "-O3", "-Wall", "-Wextra", "-Werror", *flags,
                        "-DOUTSIDE_SPEEX", "-DRANDOM_PREFIX=bluehead_speex", "-DFLOATING_POINT", "-DUSE_SSE", "-DUSE_SSE2",
                        "-I", str(vendor), "-o", output, str(source)], check=True)
        objects.append(output)
    subprocess.run([gcc, "-shared", "-static-libgcc", "-Wl,--no-insert-timestamp", "-s", "-o",
                    str(root / "bluehead/assets/bluehead_audio.dll"), *objects], check=True)

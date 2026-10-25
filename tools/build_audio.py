"""Build the allocation-free render core with MinGW-w64."""
from pathlib import Path
import shutil
import subprocess

root = Path(__file__).resolve().parents[1]
gcc = shutil.which("gcc") or "D:/Code/MSYS2/ucrt64/bin/gcc.exe"
subprocess.run([
    gcc, "-shared", "-std=c11", "-O3", "-Wall", "-Wextra", "-Werror",
    "-static-libgcc", "-Wl,--no-insert-timestamp", "-o",
    str(root / "bluehead/assets/bluehead_audio.dll"), str(root / "native/audio_mixer.c"),
], check=True)

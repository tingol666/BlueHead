"""Build vendored, patched native sources without downloading or changing drivers."""
from pathlib import Path
import subprocess
from native_build import compiler

root = Path(__file__).resolve().parents[1]
vendor = root / "vendor/libldacdec"
sources = ["bridge.c", "libldacdec.c", "bit_allocation.c", "huffCodes.c", "bit_reader.c", "utility.c", "imdct.c", "spectrum.c"]
subprocess.run([
    compiler(), "-shared", "-std=gnu11", "-O2", "-static", "-static-libgcc", "-Wl,--no-insert-timestamp",
    "-o", str(root / "bluehead/assets/bluehead_ldac.dll"), *[str(vendor / f) for f in sources], "-lm",
], check=True)

"""Build vendored, patched native sources without downloading or changing drivers."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
vendor = root / "vendor/libldacdec"
sources = ["bridge.c", "libldacdec.c", "bit_allocation.c", "huffCodes.c", "bit_reader.c", "utility.c", "imdct.c", "spectrum.c"]
subprocess.run([
    "gcc", "-shared", "-std=gnu11", "-O2", "-static", "-static-libgcc", "-Wl,--no-insert-timestamp",
    "-o", str(root / "bluehead/assets/bluehead_ldac.dll"), *[str(vendor / f) for f in sources], "-lm",
], check=True)
encoder = root / "vendor/libldac_encoder"
subprocess.run([
    "gcc", "-shared", "-std=gnu11", "-O2", "-static-libgcc", "-DLDACBT_API=__declspec(dllexport)",
    "-I" + str(encoder / "inc"), "-I" + str(encoder / "src"),
    "-o", str(root / "tests/ldac_encoder.dll"), str(encoder / "src/ldaclib.c"), str(encoder / "src/ldacBT.c"), "-lm",
], check=True)

"""Build the offline native virtual-clock checker into work/, not the release."""
from pathlib import Path
from native_build import compiler as find_compiler, includes
import subprocess

ROOT = Path(__file__).resolve().parents[1]
output = ROOT / "work/clock-stress"
output.mkdir(parents=True, exist_ok=True)
compiler = find_compiler(cxx=True)
if not (ROOT / "work/runtime-build/runtime_version.h").exists():
    raise SystemExit("Run tools/build_runtime.py first")
subprocess.run([compiler, "-std=c++17", "-O3", "-Wall", "-Wextra", "-Werror", "-municode",
                "-static", "-s", "-Wl,--no-insert-timestamp",
                *includes(ROOT / "vendor/nlohmann-json"),
                "-I", str(ROOT / "work/runtime-build"), str(ROOT / "native/testing/clock_stress.cpp"),
                "-o", str(output / "clock-stress.exe"), "-ladvapi32", "-lavrt", "-lole32"], check=True)
print(output / "clock-stress.exe")

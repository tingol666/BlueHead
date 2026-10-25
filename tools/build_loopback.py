"""Build the Windows process-excluding capture DLL with MinGW-w64."""
from pathlib import Path
import shutil
import subprocess

root = Path(__file__).resolve().parents[1]
compiler = shutil.which("g++") or "D:/Code/MSYS2/ucrt64/bin/g++.exe"
subprocess.run([compiler, "-shared", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                "-static-libgcc", "-static-libstdc++", "-Wl,--no-insert-timestamp",
                "-o", str(root / "bluehead/assets/bluehead_loopback.dll"),
                str(root / "native/process_loopback.cpp"), "-lole32", "-lmmdevapi", "-lavrt", "-luuid"], check=True)

notices = Path(compiler).resolve().parents[1] / "share/licenses/gcc-libs"
if notices.is_dir():
    shutil.copytree(notices, root / "licenses/gcc-runtime", dirs_exist_ok=True)
elif not (root / "licenses/gcc-runtime/COPYING.RUNTIME").is_file():
    raise SystemExit("Include the matching GCC runtime license and exception before distribution")

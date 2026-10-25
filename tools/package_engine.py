"""Freeze the headless receiver. The WPF desktop owns its redirected pipes."""
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
output = Path(sys.argv[1]).resolve()
build = Path(sys.argv[2]).resolve()
if (output / "BlueHead.Engine").exists():
    raise SystemExit("Choose a new engine output directory")
assets = root / "bluehead/assets"
command = [
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--console", "--onedir",
    "--name", "BlueHead.Engine", "--distpath", str(output), "--workpath", str(build), "--specpath", str(build),
    "--collect-all", "av", "--collect-submodules", "bumble", "--collect-submodules", "winrt",
    "--exclude-module", "tkinter", "--exclude-module", "pytest", "--exclude-module", "matplotlib",
    "--paths", str(root),
]
for name in ("bluehead_ldac.dll", "bluehead_audio.dll", "bluehead_loopback.dll", "libusb-1.0.dll"):
    command.extend(["--add-binary", str(assets / name) + ";bluehead/assets"])
command.append(str(root / "engine_launch.py"))
subprocess.run(command, cwd=root, check=True)

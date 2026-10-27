"""Freeze the headless receiver. The WPF desktop owns its redirected pipes."""
from pathlib import Path
import subprocess
import sys
import tomllib
import hashlib
import json
import shutil
from firmware_assets import verify as verify_firmware

root = Path(__file__).resolve().parents[1]
output = Path(sys.argv[1]).resolve()
build = Path(sys.argv[2]).resolve()
if (output / "BlueHead.Engine").exists():
    raise SystemExit("Choose a new engine output directory")
assets = root / "bluehead/assets"
usb_origin = json.loads((root / "vendor/libusb/source.json").read_text("utf-8"))
if hashlib.sha256((assets / "libusb-1.0.dll").read_bytes()).hexdigest() != usb_origin["binary_sha256"]:
    raise RuntimeError("The verified libusb runtime asset does not match its pinned source record")
version = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]["version"]
numeric = tuple(map(int, version.split("."))) + (0,)
build.mkdir(parents=True, exist_ok=True)
version_file = build / "engine-version.txt"
version_file.write_text(f'''VSVersionInfo(
  ffi=FixedFileInfo(filevers={numeric!r}, prodvers={numeric!r}, mask=0x3f,
                   flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'BlueHead contributors'),
    StringStruct('FileDescription', 'BlueHead native audio receiver control'),
    StringStruct('FileVersion', {version!r}), StringStruct('ProductVersion', {version!r}),
    StringStruct('ProductName', 'BlueHead'), StringStruct('OriginalFilename', 'BlueHead.Engine.exe')
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])])
''', "utf-8")
command = [
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--console", "--onedir",
    "--name", "BlueHead.Engine", "--distpath", str(output), "--workpath", str(build), "--specpath", str(build),
    "--version-file", str(version_file),
    "--collect-submodules", "winrt",
    "--exclude-module", "tkinter", "--exclude-module", "pytest", "--exclude-module", "matplotlib",
    "--exclude-module", "bumble.apps", "--exclude-module", "bumble.tools",
    "--exclude-module", "av", "--exclude-module", "numpy", "--exclude-module", "sounddevice",
    "--exclude-module", "tests",
    "--additional-hooks-dir", str(root / "tools/freeze_hooks"),
    "--paths", str(root),
]
command.extend(["--add-data", str(assets / "selfcheck_vectors.json") + ";bluehead/assets"])
for firmware in verify_firmware():
    destination = Path('bluehead/assets') / firmware.relative_to(assets).parent
    command.extend(["--add-data", str(firmware) + ";" + destination.as_posix()])
for name in ("bluehead_ldac.dll", "bluehead_ldac_media.dll", "bluehead_audio.dll", "bluehead_device.dll",
             "bluehead_voice.dll", "bluehead_plc.dll", "bluehead_loopback.dll", "bluehead_trace.dll",
             "avcodec-63.dll", "avutil-61.dll", "swresample-7.dll", "libwinpthread-1.dll", "libusb-1.0.dll"):
    if not (assets / name).is_file():
        raise FileNotFoundError("Build the required native component first: " + name)
    command.extend(["--add-binary", str(assets / name) + ";bluehead/assets"])
command.append(str(root / "engine_launch.py"))
log_path = build / "package.log"
with log_path.open("w", encoding="utf-8") as log:
    result = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT)
if result.returncode:
    print("\n".join(log_path.read_text("utf-8", errors="replace").splitlines()[-35:]))
    raise SystemExit(result.returncode)
# PyInstaller scans native dependencies into _internal. Keep one replaceable
# canonical FFmpeg copy there; its Windows bootloader adds that DLL directory.
# Only remove a byte-identical duplicate produced by this build.
internal = output / "BlueHead.Engine/_internal"
# The usb/usb1 freeze hooks collect their wheels' older libusb copies. Keep
# their documented search locations compatible, but make every path use the
# same verified MS64 runtime as the owned-buffer Windows transport.
for library in internal.rglob("libusb-1.0.dll"):
    shutil.copy2(assets / "libusb-1.0.dll", library)
for name in ("avcodec-63.dll", "avutil-61.dll", "swresample-7.dll", "libwinpthread-1.dll", "bluehead_plc.dll"):
    canonical, duplicate = internal / name, internal / "bluehead/assets" / name
    if canonical.is_file() and duplicate.is_file():
        if hashlib.sha256(canonical.read_bytes()).digest() != hashlib.sha256(duplicate.read_bytes()).digest():
            raise RuntimeError("Native dependency collision: " + name)
        duplicate.unlink()
shutil.copy2(assets / "BlueHead.Runtime.exe", output / "BlueHead.Engine/BlueHead.Runtime.exe")
print(output / "BlueHead.Engine")

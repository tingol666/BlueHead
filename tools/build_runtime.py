"""Build the native USB/audio runtime from pinned local dependencies."""
from pathlib import Path
from native_build import compiler as find_compiler, includes
import hashlib
import json
import subprocess
import tarfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "work/runtime-build"
BUILD.mkdir(parents=True, exist_ok=True)
vendor = ROOT / "vendor/nlohmann-json"
record = json.loads((vendor / "origin.json").read_text("utf-8-sig"))
for name, digest in record["files_sha256"].items():
    if hashlib.sha256((vendor / name).read_bytes()).hexdigest() != digest:
        raise RuntimeError("Modified pinned JSON source: " + name)
usb = ROOT / "vendor/libusb"
usb_source = usb / "libusb-1.0.30.tar.bz2"
usb_origin = json.loads((usb / "source.json").read_text("utf-8"))
if hashlib.sha256(usb_source.read_bytes()).hexdigest() != usb_origin["source_sha256"]:
    raise RuntimeError("Modified pinned libusb source archive")
with tarfile.open(usb_source) as package:
    member = next(m for m in package.getmembers() if m.name.endswith("/libusb/libusb.h"))
    (BUILD / "libusb.h").write_bytes(package.extractfile(member).read())
compiler = find_compiler(cxx=True)
version = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
numeric = ",".join(str(int(p)) for p in version.split(".")) + ",0"
(BUILD / "runtime_version.h").write_text(f'#define BH_RUNTIME_VERSION "{version}"\n', "utf-8")
(BUILD / "runtime.rc").write_text(f'''1 VERSIONINFO
FILEVERSION {numeric}
PRODUCTVERSION {numeric}
FILEOS 0x40004
FILETYPE 1
BEGIN
 BLOCK "StringFileInfo"
 BEGIN
  BLOCK "040904B0"
  BEGIN
   VALUE "FileDescription", "BlueHead native Bluetooth audio runtime"
   VALUE "ProductName", "BlueHead"
   VALUE "FileVersion", "{version}"
   VALUE "ProductVersion", "{version}"
   VALUE "OriginalFilename", "BlueHead.Runtime.exe"
   VALUE "LegalCopyright", "BlueHead contributors"
  END
 END
 BLOCK "VarFileInfo"
 BEGIN
  VALUE "Translation", 0x409, 1200
 END
END
''', "utf-8")
windres = Path(compiler).resolve().parent / "windres.exe"
subprocess.run([str(windres), str(BUILD / "runtime.rc"), str(BUILD / "runtime.o")], check=True)
subprocess.run([
    compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-municode",
    "-static", "-static-libgcc", "-static-libstdc++", "-Wl,--no-insert-timestamp", "-s",
    *includes(vendor, BUILD),
    str(ROOT / "native/runtime/runtime_main.cpp"), str(BUILD / "runtime.o"),
    "-o", str(ROOT / "bluehead/assets/BlueHead.Runtime.exe"),
    "-ladvapi32", "-lavrt", "-lole32", "-lbcrypt",
], check=True)

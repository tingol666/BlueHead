"""Rebuild the exact FFmpeg ABI with only our audio codecs; no GPL video libs.

Requires an MSYS2 UCRT64 GCC toolchain and GNU make. Builds a local copy, then
rewrites the PyAV wheel's PE import names to these replaceable canonical DLLs.
The original wheel is backed up under work/ before it is adapted.
"""
import hashlib
import importlib.util
import shlex

import pefile
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from native_build import compiler as find_compiler

native_only = "--native-only" in sys.argv[1:]
if any(argument != "--native-only" for argument in sys.argv[1:]):
    raise SystemExit("Usage: build_ffmpeg.py [--native-only]")

root = Path(__file__).resolve().parents[1]
build = root / "work/ffmpeg-audio"
build.mkdir(parents=True, exist_ok=True)
source = root / "vendor/ffmpeg/ffmpeg-9.0.2.tar.xz"
metadata = json.loads((source.parent / "source.json").read_text("utf-8"))
assert hashlib.sha256(source.read_bytes()).hexdigest() == metadata["sha256"]
if not (build / "ffmpeg-9.0.2/configure").exists():
    with tarfile.open(source) as archive:
        archive.extractall(build, filter="data")
compiler = find_compiler()
msys = Path(compiler).resolve().parents[2]
bash = msys / "usr/bin/bash.exe"
make = Path(os.environ.get("MAKE") or shutil.which("make") or str(msys / "usr/bin/make.exe"))
if not bash.exists() or not make.exists():
    raise SystemExit("MSYS2 UCRT64 GCC, bash and GNU make are required")


def posix(path):
    value = str(Path(path).resolve()).replace("\\", "/")
    return "/" + value[0].lower() + value[2:] if value[1:3] == ":/" else value


flags = [
    "--disable-everything", "--disable-autodetect", "--disable-programs", "--disable-doc",
    "--disable-debug", "--disable-network", "--disable-static", "--enable-shared", "--disable-x86asm",
    "--disable-pthreads", "--enable-w32threads", "--enable-avcodec", "--enable-avformat", "--enable-avfilter",
    "--enable-avdevice", "--enable-swscale", "--enable-swresample",
    "--enable-decoder=aac,aac_latm,sbc,aptx,aptx_hd,pcm_s16le",
    "--enable-encoder=aac,sbc,aptx,aptx_hd,pcm_s16le", "--enable-parser=aac,aac_latm,sbc",
    "--enable-filter=abuffer,abuffersink,aformat,aresample",
    "--target-os=mingw32", "--arch=x86_64", "--cc=gcc", "--extra-ldflags=-static-libgcc",
    "--prefix=" + posix(build / "install"),
]
environment = dict(os.environ, MSYSTEM="UCRT64", CHERE_INVOKING="1")
environment["PATH"] = str(msys / "ucrt64/bin") + os.pathsep + str(msys / "usr/bin") + os.pathsep + environment.get("PATH", "")
script = build / "build.sh"
script.write_text("set -eu\nexport PATH=/ucrt64/bin:/usr/bin:$PATH\ncd " + shlex.quote(posix(build / "ffmpeg-9.0.2")) +
                  "\n./configure " + " ".join(shlex.quote(f) for f in flags) + "\n" + shlex.quote(posix(make)) +
                  f" -j{min(12, os.cpu_count() or 4)}\n" + shlex.quote(posix(make)) + " install\n", "utf-8")
log = build / "build.log"
configuration = build / "configuration.json"
if not (build / "install/bin/avcodec-63.dll").exists() or not configuration.exists() or json.loads(configuration.read_text("utf-8")) != flags:
    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run([str(bash), str(script)], env=environment, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        print(log.read_text("utf-8", errors="replace")[-5000:])
        raise SystemExit(result.returncode)
    configuration.write_text(json.dumps(flags), "utf-8")

# The release engine uses the native API directly. Test environments can also
# adapt PyAV as an independent codec oracle with the same pinned FFmpeg ABI.
if native_only:
    assets = root / "bluehead/assets"
    for name in ("avcodec-63.dll", "avutil-61.dll", "swresample-7.dll"):
        shutil.copy2(build / "install/bin" / name, assets / name)
    shutil.copy2(msys / "ucrt64/bin/libwinpthread-1.dll", assets / "libwinpthread-1.dll")
    licenses = root / "licenses/ffmpeg"
    licenses.mkdir(parents=True, exist_ok=True)
    for name in ("COPYING.LGPLv2.1", "COPYING.LGPLv3", "LICENSE.md"):
        shutil.copy2(build / "ffmpeg-9.0.2" / name, licenses / name)
    report = {"version": "9.0.2", "source_sha256": metadata["sha256"], "patches": [], "configure": flags,
              "libraries": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in assets.glob("*.dll") if p.name in {
                                "avcodec-63.dll", "avutil-61.dll", "swresample-7.dll", "libwinpthread-1.dll"}},
              "scope": "Native audio-only LGPL libraries; no PyAV runtime"}
    (build / "build-report.json").write_text(json.dumps(report, indent=2) + "\n", "utf-8")
    print(json.dumps(report))
    raise SystemExit(0)

# Avoid importing av before its DLLs are adapted in this process.
av_package = Path(importlib.util.find_spec("av").origin).parent
if not av_package.resolve().is_relative_to((root / ".venv").resolve()):
    raise SystemExit("Activate the project .venv; global Python packages are never modified")
libraries = av_package.parent / "av.libs"
backup = root / "work/pyav-original-wheel"
if not (backup / "av").exists():
    shutil.copytree(av_package, backup / "av")
    shutil.copytree(libraries, backup / "av.libs")
names = {}
for file in (backup / "av.libs").glob("*.dll"):
    for stem in ("avcodec-63", "avformat-63", "avutil-61", "avfilter-12", "avdevice-63", "swscale-10", "swresample-7"):
        if file.name.startswith(stem + "-"):
            names[file.name.encode()] = (stem + ".dll").encode()
for path in av_package.rglob("*.pyd"):
    data = bytearray(path.read_bytes())
    image = pefile.PE(data=data)
    for entry in image.DIRECTORY_ENTRY_IMPORT:
        replacement = names.get(entry.dll)
        if replacement:
            offset = image.get_offset_from_rva(entry.struct.Name)
            data[offset:offset + len(entry.dll) + 1] = replacement.ljust(len(entry.dll) + 1, b"\0")
    image.close()
    path.write_bytes(data)
for path in libraries.glob("*.dll"):
    path.unlink()  # Owned build environment, originals preserved above.
for path in (build / "install/bin").glob("*.dll"):
    shutil.copy2(path, libraries / path.name)
shutil.copy2(msys / "ucrt64/bin/libwinpthread-1.dll", libraries / "libwinpthread-1.dll")
pthread_license = root / "licenses/winpthreads"
pthread_license.mkdir(parents=True, exist_ok=True)
shutil.copy2(msys / "ucrt64/share/licenses/libwinpthread/COPYING", pthread_license / "COPYING")
ffmpeg_licenses = root / "licenses/ffmpeg"
ffmpeg_licenses.mkdir(parents=True, exist_ok=True)
for name in ("COPYING.LGPLv2.1", "COPYING.LGPLv3", "LICENSE.md"):
    shutil.copy2(build / "ffmpeg-9.0.2" / name, ffmpeg_licenses / name)
order = ["libwinpthread-1.dll", "avutil-61.dll", "swresample-7.dll", "swscale-10.dll", "avcodec-63.dll", "avformat-63.dll", "avfilter-12.dll", "avdevice-63.dll"]
for path in libraries.glob(".load-order*"):
    path.write_text("\n".join(order) + "\n", "utf-8")
report = {"version": "9.0.2", "source_sha256": metadata["sha256"], "patches": [], "configure": flags,
          "libraries": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in libraries.glob("*.dll")},
          "scope": "Audio-only LGPL build; external video/GPL libraries not included"}
(build / "build-report.json").write_text(json.dumps(report, indent=2) + "\n", "utf-8")
subprocess.run([sys.executable, "-c", "import av; print(av.ffmpeg_version_info); assert all(v['license'].startswith('LGPL') for v in av._core.library_meta.values())"], check=True)
print(json.dumps({"audio_ffmpeg": True, "dll_count": len(report["libraries"]), "bytes": sum(p.stat().st_size for p in libraries.glob('*.dll'))}))

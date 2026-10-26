"""Build the allocation-free render core with MinGW-w64."""
from pathlib import Path
from native_build import compiler as find_compiler, includes
import hashlib
import json
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
gcc = find_compiler()
vendor = root / "vendor/speexdsp"
origin = json.loads((vendor / "origin.json").read_text("utf-8"))
for component in (vendor, root / "vendor/pipewire-rate-control"):
    pinned = json.loads((component / "origin.json").read_text("utf-8"))
    for name, expected in pinned["files_sha256"].items():
        if hashlib.sha256((component / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("Modified pinned native source: " + str(component / name))
with tempfile.TemporaryDirectory(prefix="bluehead-audio-") as build:
    objects = []
    ffmpeg = root / "work/ffmpeg-audio/install"
    for source in (root / "native/audio/audio_mixer.c", root / "native/audio/audio_realtime.c",
                   root / "native/codecs/media_engine.c", root / "native/audio/audio_uplink.c", vendor / "resample.c"):
        output = str(Path(build) / (source.stem + ".o"))
        flags = ["-Wno-sign-compare"] if source.parent == vendor else []
        subprocess.run([gcc, "-c", "-std=c11", "-O3", "-Wall", "-Wextra", "-Werror", *flags,
                        *includes(), "-DOUTSIDE_SPEEX", "-DRANDOM_PREFIX=bluehead_speex", "-DFLOATING_POINT", "-DUSE_SSE", "-DUSE_SSE2",
                        "-I", str(vendor), "-I", str(ffmpeg / "include"), "-o", output, str(source)], check=True)
        objects.append(output)
    subprocess.run([gcc, "-shared", "-static-libgcc", "-Wl,--no-insert-timestamp", "-s",
                    "-Wl,--out-implib," + str(root / "work/bluehead_audio.dll.a"),
                    "-o", str(root / "bluehead/assets/bluehead_audio.dll"), *objects,
                    "-L", str(ffmpeg / "lib"), "-lavcodec", "-lavutil", "-lswresample"], check=True)
    for name in ("avcodec-63.dll", "avutil-61.dll", "swresample-7.dll"):
        shutil.copy2(ffmpeg / "bin" / name, root / "bluehead/assets" / name)
    pthread = Path(gcc).resolve().parent / "libwinpthread-1.dll"
    if pthread.is_file():
        shutil.copy2(pthread, root / "bluehead/assets" / pthread.name)

"""Build pinned Google LC3 + BlueHead voice framing; no audio hardware access."""

from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
from native_build import compiler as find_compiler, includes
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("attdet", "bits", "bwdet", "energy", "lc3", "ltpf", "mdct", "plc", "sns", "spec", "tables", "tns")


def verify_vendor(vendor: Path) -> None:
    origin = json.loads((vendor / "origin.json").read_text("utf-8"))
    for name, expected in origin["files_sha256"].items():
        if hashlib.sha256((vendor / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("Modified pinned source: " + str(vendor / name))


def build(output: Path | None = None, compiler: str | None = None, ffmpeg: Path | None = None) -> Path:
    gcc = compiler or find_compiler()
    vendor = ROOT / "vendor/liblc3"
    speex = ROOT / "vendor/speexdsp"
    plc = ROOT / "vendor/spandsp-plc"
    for source_vendor in (vendor, speex, plc):
        verify_vendor(source_vendor)
    ffmpeg = ffmpeg or ROOT / "work/ffmpeg-audio/install"
    if not (ffmpeg / "include/libavcodec/avcodec.h").is_file():
        raise RuntimeError("Build the pinned FFmpeg first, or use --ffmpeg <install-prefix>")
    output = output or ROOT / "bluehead/assets/bluehead_voice.dll"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bluehead-voice-") as directory:
        directory = Path(directory)
        # Keep upstream PLC algorithm unchanged. Only its platform/include
        # adapter is replaced; all original source and this reproducible port
        # are distributed. It remains a separate LGPL-2.1 replaceable DLL.
        plc_source = (plc / "src/plc.c").read_text("utf-8")
        start = plc_source.index("#if defined(HAVE_CONFIG_H)")
        end = plc_source.index("/* We do a straight line fade", start)
        plc_source = plc_source[:start] + '#include "plc_port.h"\n\n' + plc_source[end:]
        plc_translation = directory / "plc_windows.c"
        plc_translation.write_text(plc_source, "utf-8")
        plc_library = directory / "libbluehead_plc.dll.a"
        subprocess.run(
            [
                gcc,
                "-std=c11",
                "-O3",
                "-Wall",
                "-Wextra",
                "-Wno-unused-parameter",
                "-shared",
                "-static-libgcc",
                "-s",
                "-Wl,--no-insert-timestamp",
                "-Wl,--out-implib," + str(plc_library),
                "-I",
                str(plc),
                str(plc_translation),
                "-o",
                str(output.parent / "bluehead_plc.dll"),
            ],
            check=True,
        )
        objects = []
        for source in [
            ROOT / "native/codecs/voice_engine.c",
            ROOT / "native/codecs/lc3_stream.c",
            speex / "resample.c",
            *(vendor / "src" / (name + ".c") for name in SOURCES),
        ]:
            obj = directory / (source.stem + ".o")
            flags = (
                ["-Werror"] if source.is_relative_to(ROOT / "native") else ["-Wno-unused-parameter", "-Wno-sign-compare"]
            )
            subprocess.run(
                [
                    gcc,
                    "-std=c11",
                    "-O3",
                    "-Wall",
                    "-Wextra",
                    *flags,
                    *includes(),
                    "-DLC3_PLUS=0",
                    "-DLC3_PLUS_HR=0",
                    "-ffunction-sections",
                    "-fdata-sections",
                    "-DOUTSIDE_SPEEX",
                    "-DRANDOM_PREFIX=bluehead_voice_speex",
                    "-DFLOATING_POINT",
                    "-DUSE_SSE",
                    "-DUSE_SSE2",
                    "-I",
                    str(vendor / "include"),
                    "-I",
                    str(vendor / "src"),
                    "-I",
                    str(speex),
                    "-I",
                    str(ffmpeg / "include"),
                    "-c",
                    str(source),
                    "-o",
                    str(obj),
                ],
                check=True,
            )
            objects.append(str(obj))
        subprocess.run(
            [
                gcc,
                "-shared",
                "-static-libgcc",
                "-Wl,--gc-sections",
                "-Wl,--no-insert-timestamp",
                "-s",
                "-o",
                str(output),
                *objects,
                str(plc_library),
                "-L",
                str(ffmpeg / "lib"),
                "-lavcodec",
                "-lavutil",
            ],
            check=True,
        )
    print(output)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compiler")
    parser.add_argument("--ffmpeg", type=Path)
    arguments = parser.parse_args()
    build(arguments.output, arguments.compiler, arguments.ffmpeg)

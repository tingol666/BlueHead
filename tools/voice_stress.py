"""Offline actual-codec endurance test; never enumerates or opens audio devices.

python tools/voice_stress.py --minutes 30 --report work/voice-2/stress.json
Simulates the requested audio duration as fast as the real native codecs run.
"""

from __future__ import annotations
import argparse
import json
import math
import os
from pathlib import Path
import struct
import sys
import time
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    if not 0 < arguments.minutes <= 120:
        parser.error("minutes must be between 0 and 120")
    # Development builds keep pinned FFmpeg in its install prefix. Release
    # packages carry DLLs beside BlueHead's native assets.
    ffmpeg = ROOT / "work/ffmpeg-audio/install/bin"
    search = os.add_dll_directory(str(ffmpeg)) if ffmpeg.is_dir() else None
    from bluehead.voice import VoiceCodec

    frames = round(arguments.minutes * 60 / 0.0075)
    pcm = struct.pack("<360h", *(round(10000 * math.sin(2 * math.pi * 800 * i / 48000)) for i in range(360)))
    report = {"simulated_minutes_each_codec": arguments.minutes, "frames_each_codec": frames, "codecs": {}}
    try:
        for codec in (1, 2, 3):
            started = time.perf_counter()
            checksum = 0
            with VoiceCodec(codec) as voice:
                for index in range(frames):
                    packet = voice.encode(pcm)
                    # Exercise phase shifts in actual HCI packet boundaries.
                    split = 1 + index % (len(packet) - 1)
                    voice.push_rx(packet[:split])
                    voice.push_rx(packet[split:])
                    output = voice.read_pcm()
                    if len(output) != 720:
                        raise RuntimeError(f"{voice.name}: PCM clock drift at frame {index}")
                    checksum = zlib.crc32(output, checksum)
                stats = voice.stats()
                if stats["queue_overruns"] or stats["plc_frames"] or stats["queued_samples"]:
                    raise RuntimeError(f"{voice.name}: unexpected degradation {stats}")
                report["codecs"][voice.name] = {
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "checksum": checksum,
                    "stats": stats,
                }
            print(codec, report["codecs"][{1: "CVSD", 2: "mSBC", 3: "LC3-SWB"}[codec]])
    finally:
        if search:
            search.close()
    if arguments.report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(json.dumps(report, indent=2) + "\n", "utf-8")


if __name__ == "__main__":
    main()

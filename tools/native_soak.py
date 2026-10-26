"""Run reproducible native audio replay; no USB, speaker or microphone is opened.

Compressed fixtures pass through the actual native HCI reassembler/codecs/mixer.
Python only starts the replay and records counters. The optional GIL stalls do
not replace the C++ producer, renderer, voice transport or native clocks.
"""
import argparse
import ctypes as C
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bluehead.native_runtime import NativeClient  # noqa: E402

ASSETS = ("BlueHead.Runtime.exe", "bluehead_audio.dll", "bluehead_device.dll", "bluehead_voice.dll",
          "bluehead_plc.dll", "bluehead_loopback.dll", "avcodec-63.dll", "avutil-61.dll",
          "swresample-7.dll", "libwinpthread-1.dll", "selfcheck_vectors.json")


class Memory(C.Structure):
    _fields_ = [("size", C.c_uint32), ("faults", C.c_uint32),
                *[(name, C.c_size_t) for name in ("peak_working", "working", "peak_paged", "paged",
                                                 "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile", "private")]]


def resources(process):
    api = C.WinDLL("kernel32", use_last_error=True)
    api.K32GetProcessMemoryInfo.argtypes = [C.c_void_p, C.POINTER(Memory), C.c_uint32]
    api.GetProcessTimes.argtypes = [C.c_void_p] + [C.c_void_p] * 4
    counters = Memory()
    counters.size = C.sizeof(counters)
    if not api.K32GetProcessMemoryInfo(int(process._handle), C.byref(counters), counters.size):
        raise C.WinError(C.get_last_error())
    values = [C.c_uint64() for _ in range(4)]
    if not api.GetProcessTimes(int(process._handle), *(C.byref(v) for v in values)):
        raise C.WinError(C.get_last_error())
    return {"working_bytes": counters.working, "private_bytes": counters.private,
            "cpu_seconds": (values[2].value + values[3].value) / 10000000}


def save(path, report):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", "utf-8")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--codec", choices=("sbc", "aac", "aptx", "aptx_hd"), default="sbc")
    parser.add_argument("--voice-codec", type=int, choices=(0, 1, 2, 3), default=0)
    parser.add_argument("--drop-every", type=int, default=0)
    parser.add_argument("--jitter-ms", type=float, default=0)
    parser.add_argument("--clock-ppm", type=float, default=0)
    parser.add_argument("--gil-stall-ms", type=int, default=0)
    parser.add_argument("--native-stall-ms", type=int, default=0)
    parser.add_argument("--native-stall-direction", choices=("media", "voice", "capture"), default="media")
    parser.add_argument("--bounded-stall-recovery", action="store_true",
                        help="For <=20ms media stalls: require bounded gap/expiry and no rebuffer, rather than zero deadline gaps")
    parser.add_argument("--legacy-continuity", action="store_true",
                        help="Run the old buffering strategy inside the same native replay harness")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--assets", type=Path, default=ROOT / "bluehead/assets")
    parser.add_argument("--snapshot", type=Path, help="Copy exact native inputs to a new isolated directory")
    args = parser.parse_args()
    if not 0 <= args.gil_stall_ms <= 10000:
        parser.error("GIL stall must be 0–10000 ms")
    if not 0 <= args.native_stall_ms <= 1000:
        parser.error("Native worker stall must be 0–1000 ms")
    if args.bounded_stall_recovery and not (0 < args.native_stall_ms <= 20 and args.native_stall_direction == "media"):
        parser.error("Bounded recovery profile requires a 1–20ms media worker stall")
    assets = args.assets.resolve()
    if args.snapshot:
        snapshot = args.snapshot.resolve()
        snapshot.mkdir(parents=True, exist_ok=False)
        for name in ASSETS:
            shutil.copy2(assets / name, snapshot / name)
        assets = snapshot
    report = args.report.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    record = {"scope": "Offline native synthetic HCI/PCM endpoints; no USB/audio hardware opened",
              "started_utc": datetime.now(timezone.utc).isoformat(), "done": False,
              "native_sha256": {name: hashlib.sha256((assets / name).read_bytes()).hexdigest() for name in ASSETS},
              "gil_stall_ms": args.gil_stall_ms, "gil_stalls": 0,
              "native_stall_ms": args.native_stall_ms, "native_stall_direction": args.native_stall_direction,
              "native_stalls": 0,
              "validation_profile": "bounded_media_stall_recovery" if args.bounded_stall_recovery else "zero_deadline_gaps", "resources": {}}
    save(report, record)
    started = time.monotonic()
    power = C.WinDLL("kernel32").SetThreadExecutionState
    power.argtypes, power.restype = [C.c_uint32], C.c_uint32
    record["temporary_system_sleep_prevention"] = bool(power(0x80000001))
    try:
        with NativeClient(spawn=True, assets=assets, executable=assets / "BlueHead.Runtime.exe") as client:
            record["hello"] = client.request("hello")
            client.request("test_replay", seconds=args.seconds, codec=args.codec, voice_codec=args.voice_codec,
                           drop_every=args.drop_every, jitter_ms=args.jitter_ms, clock_ppm=args.clock_ppm,
                           legacy_continuity=args.legacy_continuity)
            minimum = maximum = baseline = None
            next_stall = started + 15
            next_native_stall = started + 22
            while True:
                current = client.request("test_replay_status")
                native = client.request("snapshot")
                media = client.request("media_snapshot", key="offline:media")
                used = resources(client.process)
                if time.monotonic() - started >= 10:
                    baseline = baseline or used["private_bytes"]
                    minimum = min(minimum or used["private_bytes"], used["private_bytes"])
                    maximum = max(maximum or used["private_bytes"], used["private_bytes"])
                record.update({"replay": current, "native": native, "media": media, "done": bool(current.get("done")),
                               "resources": {**used, "private_baseline": baseline, "private_min": minimum,
                                             "private_max": maximum,
                                             "cpu_one_core_percent": used["cpu_seconds"] / max(.001, time.monotonic()-started) * 100}})
                save(report, record)
                if record["done"]:
                    break
                if report.with_suffix(".stop").exists():
                    client.request("test_replay_stop")
                    raise InterruptedError("Replay stopped by its local stop-file")
                if time.monotonic() > started + args.seconds + 20:
                    raise TimeoutError("Native replay exceeded its deadline")
                if args.gil_stall_ms and time.monotonic() >= next_stall:
                    C.PyDLL("kernel32").Sleep(args.gil_stall_ms)
                    record["gil_stalls"] += 1
                    next_stall = time.monotonic() + 15
                if args.native_stall_ms and time.monotonic() >= next_native_stall:
                    client.request("test_stall", direction=args.native_stall_direction, milliseconds=args.native_stall_ms)
                    record["native_stalls"] += 1
                    next_native_stall = time.monotonic() + 15
                time.sleep(min(5, max(.05, args.seconds - current.get("elapsed_seconds", 0))))
            failures = {name: current.get(name, 0) for name in ("codec_errors", "compressed_queue_drops", "dropped_frames", "underruns")
                        if current.get(name, 0)}
            if args.bounded_stall_recovery:
                # A deliberately blocked decoder plus a lost AAC packet can
                # cross the presentation deadline even with a healthy USB
                # owner. The contract is finite recovery without replaying
                # expired PCM, not fabricating the original missing signal.
                sources = native.get("sources", {}).values()
                counters = {key: sum(source.get(key, 0) for source in sources) for key in
                            ("concealed_frames", "expired_frames", "rebuffer_events", "callback_period_searches")}
                budget = record["native_stalls"] * (args.native_stall_ms + 20) * 48
                bounded = (counters["concealed_frames"] <= budget and counters["expired_frames"] <= budget
                           and abs(counters["concealed_frames"]-counters["expired_frames"]) <= 2*len(native.get("sources", {}))
                           and counters["rebuffer_events"] == counters["callback_period_searches"] == 0
                           and current.get("maximum_zero_run_ms", 0) <= args.native_stall_ms + 10)
                record["stall_recovery"] = dict(counters, budget_frames=budget, passed=bounded,
                                                deadline_gap_events=current.get("underruns", 0))
                if bounded:
                    failures.pop("underruns", None)
                else:
                    failures["unbounded_stall_recovery"] = 1
            record["passed"] = not failures
            record["failures"] = failures
    except BaseException as error:
        record.update({"passed": False, "error": str(error)})
        raise
    finally:
        power(0x80000000)
        record["finished_utc"] = datetime.now(timezone.utc).isoformat()
        save(report, record)
    print(json.dumps({"passed": record["passed"], "report": str(report), "replay": record["replay"]}, ensure_ascii=False))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

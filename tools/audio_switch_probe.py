"""Physical endpoint switch check: output silence, count mic frames, save no audio."""
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def probe():
    import sounddevice as sd
    from bluehead.audio import AudioOutput, Mixer
    from bluehead.calls import CallRouter, Microphone

    result = {"passed": False, "scope": "silent output and microphone callback counts; no radio or audio recording"}
    devices, apis = sd.query_devices(), sd.query_hostapis()
    wasapi = next(i for i, api in enumerate(apis) if "WASAPI" in api["name"])
    outputs = [i for i, d in enumerate(devices) if d["hostapi"] == wasapi and d["max_output_channels"] >= 2
               and any(name in d["name"] for name in ("Realtek", "JM98MAX", "USB Audio"))][:2]
    inputs = [i for i, d in enumerate(devices) if d["hostapi"] == wasapi and d["max_input_channels"] >= 1
              and any(name in d["name"] for name in ("麦克风", "Microphone"))
              and not any(name in d["name"] for name in ("Voicemeeter", "VB-", "CABLE"))][:2]
    output = AudioOutput(Mixer(), None)
    router = CallRouter()
    router.select("local-check")
    microphone = None
    try:
        output.start()
        result["output_switches"] = []
        for index in outputs + [None]:
            output.switch_device(index)
            result["output_switches"].append({"device": index, "active": bool(output.stream.active)})
        microphone = Microphone(router, "local-check", 16000, None, lambda *args, **kwargs: None)
        result["microphone_switches"] = []
        for index in inputs + [None]:
            microphone.switch_device(index)
            before = microphone.frames
            time.sleep(.3)
            received = microphone.frames - before
            result["microphone_switches"].append({"device": index, "capture_frames": received})
            assert received > 0, "microphone callbacks stopped after switching"
        selected = output.device
        try:
            output.switch_device(len(devices) + 1000)
        except (sd.PortAudioError, ValueError):
            pass
        else:
            raise AssertionError("Invalid output endpoint should fail")
        result["invalid_output_rolled_back"] = output.device == selected and bool(output.stream.active)
        assert result["invalid_output_rolled_back"]
        result["passed"] = True
    finally:
        output.close()
        if microphone:
            microphone.close()
        router.select(None)
    return result


if __name__ == "__main__":
    import traceback
    try:
        report = probe()
    except Exception:
        report = {"passed": False, "error": traceback.format_exc()}
    Path("work/audio-switch-probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    raise SystemExit(0 if report["passed"] else 1)

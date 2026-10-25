"""Read-only compatibility checks. Does not open, reset, or claim a Bluetooth adapter."""
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def diagnose():
    import av
    import sounddevice as sd
    from bluehead.backend import usb_adapters
    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.media.audio import AudioPlaybackConnection
    result = {"adapters": usb_adapters(), "av_version": av.__version__}
    result["decoders"] = {c: c in av.codecs_available for c in ("aac_latm", "sbc", "aptx", "aptx_hd")}
    devices = sd.query_devices()
    result["audio_output_count"] = sum(d["max_output_channels"] >= 2 for d in devices)
    result["audio_input_count"] = sum(d["max_input_channels"] >= 1 for d in devices)
    try:
        devices = await DeviceInformation.find_all_async(AudioPlaybackConnection.get_device_selector(), [])
        result["native_source_count"] = len(devices)
    except Exception as exc:
        result["native_error"] = str(exc)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return result


if __name__ == "__main__":
    asyncio.run(diagnose())

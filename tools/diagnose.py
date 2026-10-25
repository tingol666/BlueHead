"""Native dependency checks and read-only inventory counts.

No physical audio stream, Bluetooth connection, adapter claim/reset or driver
operation is performed. Endpoint names, addresses and pairing data are omitted.
"""
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def diagnose():
    from bluehead import __version__
    result = {"version": __version__, "passed": False, "scope": __doc__.strip(),
              "native_media": {}, "native_voice": {}, "counts": {}, "errors": {}}
    if sys.platform != "win32":
        result["errors"]["platform"] = "Windows is required"
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return result
    from bluehead.media import NativeMedia
    from bluehead.protocol import Format
    for codec in ("sbc", "aac", "aptx", "aptx_hd"):
        decoder = None
        try:
            decoder = NativeMedia(Format(codec, 48000, 2))
            result["native_media"][codec] = True
        except Exception as error:
            result["native_media"][codec] = False
            result["errors"]["media_" + codec] = type(error).__name__
        finally:
            if decoder is not None:
                decoder.close()
    # LDAC is experimental and remains isolated. Presence is not a decoding test.
    assets = Path(__file__).resolve().parents[1] / "bluehead/assets"
    result["experimental_ldac"] = {"default_enabled": False, "executed": False,
        "libraries_present": all((assets / name).is_file()
            for name in ("bluehead_ldac.dll", "bluehead_ldac_media.dll"))}
    try:
        from bluehead.voice import CODEC_NAMES, available_codecs
        available = available_codecs()
        result["native_voice"] = {name: codec in available for codec, name in CODEC_NAMES.items()}
    except Exception as error:
        result["errors"]["voice_dependencies"] = type(error).__name__
    try:
        from bluehead.backend import usb_adapters
        result["counts"]["usb_hci_adapters"] = len(usb_adapters())
    except Exception as error:
        result["errors"]["usb_inventory"] = type(error).__name__
    context = None
    try:
        from bluehead.audio_device import Context
        context = Context()
        devices = context.inventory()
        result["counts"]["audio_outputs"] = sum(d["direction"] == "output" for d in devices)
        result["counts"]["audio_inputs"] = sum(d["direction"] == "input" for d in devices)
    except Exception as error:
        result["errors"]["wasapi_inventory"] = type(error).__name__
    finally:
        if context is not None:
            try:
                context.close()
            except Exception as error:
                result["errors"]["wasapi_close"] = type(error).__name__
    try:
        from winrt.windows.devices.enumeration import DeviceInformation
        from winrt.windows.media.audio import AudioPlaybackConnection
        devices = await DeviceInformation.find_all_async(AudioPlaybackConnection.get_device_selector(), [])
        result["counts"]["windows_paired_audio_sources"] = len(devices)
    except Exception as error:
        result["errors"]["windows_source_inventory"] = type(error).__name__
    result["passed"] = (not result["errors"] and all(result["native_media"].values())
                        and bool(result["native_voice"]) and all(result["native_voice"].values()))
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return result


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(diagnose())["passed"] else 1)

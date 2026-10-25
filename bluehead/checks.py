"""Read-only packaged-program smoke test; opens no radio links or microphones."""
import asyncio
import json
from pathlib import Path
import tempfile
import traceback


def smoke_test(path):
    result = {"passed": False, "scope": "packaged imports, decoders, USB enumeration, native enumeration, UI; no radio connection or physical microphone"}
    try:
        import numpy as np
        from .calls import Msbc
        from .backend import usb_adapters
        from .ldac import NativeLdac, LdacDecoder
        from .app import App
        import tkinter as tk
        codec = Msbc()
        packet = codec.encode(np.zeros(120, dtype="<i2").tobytes())
        assert sum(len(a) for a in codec.decode(packet)) == 120
        result["msbc_round_trip"] = True
        decoder = NativeLdac()
        decoder.close()
        worker = LdacDecoder(48000)
        worker.close()
        result["isolated_ldac_worker"] = True
        result["adapters"] = usb_adapters()
        import usb1
        version = usb1.getVersion()
        result["native_libusb"] = f"{version.major}.{version.minor}.{version.micro}"
        assert result["native_libusb"] == "1.0.30"
        import sounddevice as sd
        result["audio_endpoints"] = len(sd.query_devices())
        async def native_enumeration():
            def worker():
                async def check():
                    from winrt.windows.devices.enumeration import DeviceInformation
                    from winrt.windows.media.audio import AudioPlaybackConnection
                    from .native import NativeReceiver
                    stop = asyncio.Event()
                    stop.set()
                    await NativeReceiver(lambda *args, **kwargs: None).run(stop)
                    return len(await DeviceInformation.find_all_async(AudioPlaybackConnection.get_device_selector(), []))
                return asyncio.run(check())
            return await asyncio.to_thread(worker)
        result["native_sources"] = asyncio.run(native_enumeration())
        result["native_lifecycle"] = True
        root = tk.Tk()
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory(prefix="bluehead-check-") as directory:
                app = App(root, Path(directory), scan=False)
                app.emit("device", key="test", state="已连接")
                app.emit("format", key="test", codec="aac", rate=48000, channels=2)
                app.drain()
                assert "AAC" in app.table.item("test", "values")[1]
                root.update_idletasks()
            result["desktop_ui"] = True
        finally:
            root.destroy()
        result["passed"] = True
    except Exception:
        result["error"] = traceback.format_exc()
    Path(path).write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    return 0 if result["passed"] else 1

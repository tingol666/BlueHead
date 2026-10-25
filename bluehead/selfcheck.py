"""Frozen-package audio dependency check. Uses generated PCM, never opens devices."""
import json
from pathlib import Path

import av
import numpy as np

from . import __version__
from .audio import Mixer
from .calls import Msbc
from .ldac import LdacDecoder
from .realtime import NativeCapture, NativeRender


def run(report):
    checks = {}
    for codec in ("sbc", "aac_latm", "aptx", "aptx_hd"):
        context = av.CodecContext.create(codec, "r")
        context.sample_rate = 48000
        context.layout = "stereo"
        context.open()
        checks["decoder_"+codec] = True
    voice = Msbc()
    checks["msbc_encode_decode"] = len(voice.decode(voice.encode(bytes(240)))[0]) == 120
    decoder = LdacDecoder(48000)
    decoder.close()
    checks["isolated_ldac_initializes"] = True
    for rate in (44100, 48000, 88200, 96000, 192000):
        mixer = Mixer(buffer_ms=0)
        mixer.add("generated")
        mixer.write("generated", np.full((4800, 2), .125, np.float32))
        renderer = NativeRender(mixer.native, rate)
        output = np.empty((rate//100, 2), np.float32)
        renderer.render_into(output)
        checks[f"native_render_{rate}"] = bool(np.isfinite(output).all() and np.mean(output) > .1)
        renderer.close()
    for rate in (8000, 16000):
        capture = NativeCapture(48000, 2, rate)
        capture.push(np.full((2400, 2), .125, np.float32))
        output = np.empty(1024, np.int16)
        count = capture.read_into(output)
        checks[f"native_capture_{rate}"] = bool(count == rate//20 and np.max(output[:count]) > 3000)
        capture.close()
    # Imports validate projections and callback ABI; no apartment, selector,
    # stream, adapter, microphone or loopback session is opened.
    import sounddevice as sd
    from winrt.windows.media.audio import AudioPlaybackConnection
    from winrt.windows.devices.enumeration import DeviceInformation
    from .loopback import ProcessCapture
    checks["winrt_projections"] = all(value is not None for value in (AudioPlaybackConnection, DeviceInformation))
    checks["process_capture_module"] = ProcessCapture is not None
    checks["native_callback_abi"] = sd._ffi.sizeof("unsigned long") == 4 and sd._ffi.sizeof("void*") == 8
    result = {"version": __version__, "scope": "Generated PCM and dependency imports only; no hardware opened",
              "passed": all(checks.values()), "checks": checks}
    Path(report).write_text(json.dumps(result, indent=2), "utf-8")
    return 0 if result["passed"] else 1

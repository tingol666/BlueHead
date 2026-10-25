"""Frozen native dependency/codec self-check. Never opens audio or Bluetooth."""
import base64
import ctypes as c
import hashlib
import json
import math
from pathlib import Path
import struct
import sys

from . import __version__


def run(report):
    from .audio import Decoder, Mixer
    from .audio_device import Context, PLAYBACK, CAPTURE
    from .protocol import Format
    from .voice import VoiceCodec
    checks, errors = {}, {}

    def check(name, function):
        try:
            checks[name] = bool(function())
        except Exception as error:
            checks[name] = False
            errors[name] = f"{type(error).__name__}: {error}"

    vectors = json.loads((Path(__file__).parent / "assets/selfcheck_vectors.json").read_text("utf-8"))
    for vector in vectors["vectors"]:
        def media(vector=vector):
            packets = [base64.b64decode(value, validate=True) for value in vector["packets"]]
            if hashlib.sha256(b"".join(packets)).hexdigest() != vector["sha256"]:
                return False
            decoder = Decoder(Format(vector["codec"], vector["rate"], vector["channels"]))
            try:
                pcm = bytearray()
                for packet in packets:
                    decoder.decode_packet(packet)
                    pcm.extend(decoder.read_pcm())
                values = [sample[0] for sample in struct.iter_unpack("<f", pcm)]
                return len(values) > 6000 and all(math.isfinite(value) for value in values) and max(map(abs, values)) > .01
            finally:
                decoder.close()
        check("native_media_" + vector["codec"], media)

    tone = struct.pack("<360h", *(round(4000 * math.sin(2 * math.pi * 997 * i / 48000)) for i in range(360)))
    for codec_id in (1, 2, 3):
        def voice(codec_id=codec_id):
            with VoiceCodec(codec_id) as codec:
                pcm = bytearray()
                for _ in range(24):
                    wire = codec.encode(tone)
                    codec.push_rx(wire[:17])
                    codec.push_rx(wire[17:])
                    pcm.extend(codec.read_pcm())
                stats = codec.stats()
                return (len(pcm) == 24 * 720 and stats["queue_overruns"] == 0 and
                        max(abs(sample[0]) for sample in struct.iter_unpack("<h", pcm)) > 100)
        check("native_voice_" + str(codec_id), voice)

    context = Context()  # DLL loading only; native context is lazy.
    try:
        for rate in (44100, 48000, 88200, 96000, 192000):
            def render(rate=rate):
                mixer = Mixer(buffer_ms=0)
                mixer.add("generated")
                source = struct.pack("<f", .125) * (4800 * 2)
                mixer.write_float_bytes("generated", source)
                stream = context._offline(PLAYBACK, mixer=mixer, rate=rate, channels=2)
                try:
                    stream.start()
                    count = rate // 100
                    output = (c.c_float * (count * 2))()
                    result = context.api.bh_device_offline_process(stream.handle, None, output, count)
                    return result == 0 and all(math.isfinite(value) for value in output) and sum(output) / len(output) > .1
                finally:
                    stream.close()
            check("native_endpoint_render_" + str(rate), render)
        for rate in (8000, 16000, 32000, 48000):
            def capture(rate=rate):
                stream = context._offline(CAPTURE, rate=48000, channels=2, output_rate=rate)
                try:
                    stream.start()
                    source = (c.c_float * 4800)(*([.125] * 4800))
                    result = context.api.bh_device_offline_process(stream.handle, source, None, 2400)
                    pcm = bytearray()
                    while (data := stream.read(1024)):
                        pcm.extend(data)
                    return result == 0 and len(pcm) == rate // 20 * 2 and max(sample[0] for sample in struct.iter_unpack("<h", pcm)) > 3000
                finally:
                    stream.close()
            check("native_endpoint_capture_" + str(rate), capture)
    finally:
        context.close()

    def projections():
        from winrt.windows.media.audio import AudioPlaybackConnection
        from winrt.windows.devices.enumeration import DeviceInformation
        from .loopback import ProcessCapture
        return all(value is not None for value in (AudioPlaybackConnection, DeviceInformation, ProcessCapture))
    check("windows_projections", projections)
    def usb_library():
        from .windows_usb import load_libusb
        load_libusb()  # DLL version only; no USB context, reset, or interface claim.
        return True
    check("verified_libusb_library", usb_library)
    def uplink():
        from .uplink import CallRouter
        router = CallRouter()
        try:
            router.configure(48000, adaptive=True)
            router.select("generated")
            for value, write in ((3000, router.write_microphone), (2000, router.write_system)):
                write("generated", struct.pack("<h", value) * 2400)
            output = router.read("generated", 720)
            state = router.snapshot()
            return (state["adaptive"] and state["buffer_reference"] == "render_center" and
                    all(state[bus]["dropped_frames"] == state[bus]["underruns"] == 0 for bus in ("microphone", "system")) and
                    max(sample[0] for sample in struct.iter_unpack("<h", output)) > 4500)
        finally:
            router.close()
    check("native_dual_capture_clock", uplink)
    check("no_python_dsp_runtime", lambda: not {"av", "numpy", "sounddevice"}.intersection(sys.modules))
    check("x64_abi", lambda: c.sizeof(c.c_void_p) == 8 and c.sizeof(c.c_ulong) == 4)
    result = {"version": __version__, "scope": "Synthetic codec vectors and native in-memory endpoints only; no hardware opened",
              "passed": all(checks.values()), "checks": checks, "errors": errors}
    Path(report).write_text(json.dumps(result, indent=2), "utf-8")
    return 0 if result["passed"] else 1

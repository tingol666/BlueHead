"""Native WASAPI callback/lifetime checks; no physical device is opened."""
import ctypes as C
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
import threading

import pytest

from bluehead import audio_device as device


@pytest.fixture
def context():
    value = device.Context()
    yield value
    value.close()


@pytest.fixture
def mixer():
    api = C.CDLL(str((Path(device.__file__).parent / "assets/bluehead_audio.dll").resolve()))
    api.bh_mix_create.restype = C.c_void_p
    api.bh_mix_free.argtypes = [C.c_void_p]
    api.bh_mix_add.argtypes = [C.c_void_p, C.c_uint32, C.c_uint32]
    api.bh_mix_write.argtypes = [C.c_void_p, C.c_int, C.c_void_p, C.c_uint32]
    handle = api.bh_mix_create()
    index = api.bh_mix_add(handle, 0, 48000)
    yield api, handle, index
    api.bh_mix_free(handle)


def test_context_is_lazy_and_rejects_relative_core_path(context):
    error = C.c_int()
    assert not context.api.bh_device_context_create("bluehead_audio.dll", C.byref(error))
    assert error.value < 0
    assert context._worker.daemon
    assert context._call(threading.get_ident) == context._thread
    assert context._thread != threading.get_ident()


@pytest.mark.parametrize("rate", [8000, 16000, 44100, 48000, 96000, 192000, 384000])
def test_native_output_callback_uses_stateful_core_without_python(context, mixer, rate):
    api, handle, index = mixer
    stream = context._offline(device.PLAYBACK, mixer=handle, rate=rate, channels=2)
    pcm = (C.c_float * 48000)(*([.2, -.1] * 24000))
    api.bh_mix_write(handle, index, pcm, 24000)
    output = (C.c_float * (rate // 4 * 2))()
    for offset in range(0, rate // 4, 1137):
        assert context.api.bh_device_offline_process(stream.handle, None, C.byref(output, offset*8), min(1137, rate//4-offset)) == 0
    skip = rate // 20 * 2
    assert max(abs(value) for value in output[skip:]) < .21
    assert sum(output[skip::2]) / len(output[skip::2]) == pytest.approx(.2, abs=.002)
    assert sum(output[skip+1::2]) / len(output[skip+1::2]) == pytest.approx(-.1, abs=.002)
    assert stream.snapshot()["conversion_errors"] == 0
    stream.close()


@pytest.mark.parametrize("rate,channels,output_rate", [(48000, 1, 48000), (96000, 2, 48000), (44100, 2, 16000), (48000, 1, 32000)])
def test_capture_callback_queue_downmix_and_native_src(context, rate, channels, output_rate):
    stream = context._offline(device.CAPTURE, rate=rate, channels=channels, output_rate=output_rate)
    pieces = []
    for block in range(100):
        values = []
        for frame in range(rate // 100):
            value = .2 * math.sin(2 * math.pi * 1000 * (block*(rate//100)+frame) / rate)
            values.extend([value] if channels == 1 else [value, value*.5])
        pcm = (C.c_float * len(values))(*values)
        assert context.api.bh_device_offline_process(stream.handle, pcm, None, rate // 100) == 0
        while data := stream.read():
            pieces.append(data)
    samples = struct.unpack("<" + "h" * (sum(map(len, pieces)) // 2), b"".join(pieces))
    assert len(samples) == output_rate
    normalized = [value / 32768 for value in samples[output_rate//10:]]
    rms = math.sqrt(sum(value*value for value in normalized) / len(normalized))
    assert rms == pytest.approx(.2*(1 if channels == 1 else .75)/math.sqrt(2), rel=.015)
    assert stream.snapshot()["dropped_frames"] == 0
    stream.close()


def test_capture_overflow_and_stop_never_replay_stale_speech(context):
    stream = context._offline(device.CAPTURE)
    pcm = (C.c_float * 4000)(*([.25] * 4000))
    context.api.bh_device_offline_process(stream.handle, pcm, None, 4000)
    assert stream.snapshot()["queued_frames"] <= 2880
    assert stream.snapshot()["dropped_frames"] == 1120
    assert stream.read() == b""
    context.api.bh_device_offline_process(stream.handle, pcm, None, 480)
    assert len(stream.read()) == 960
    context.api.bh_device_offline_process(stream.handle, pcm, None, 480)
    stream.start().stop()
    assert not stream.active and stream.read() == b""
    stream.close()


def test_abi_bounds_context_owner_and_stream_shutdown(context):
    assert C.sizeof(device.Endpoint) == 400
    assert C.sizeof(device.DeviceStats) == 96
    stream = context._offline(device.CAPTURE)
    assert context.api.bh_device_context_close(context.handle) < 0
    assert context.api.bh_device_offline_process(stream.handle, None, None, device.MAX_FRAMES+1) < 0
    bad = device.DeviceStats(size=1)
    assert context.api.bh_device_stats(stream.handle, C.byref(bad)) < 0
    for size in (0, -1, device.MAX_FRAMES+1, 1.5):
        with pytest.raises(ValueError):
            stream.read(size)
    stream.start()
    assert stream.active
    stream.close()
    stream.close()
    assert not stream.active and stream.snapshot()["active"] == 0
    with pytest.raises(RuntimeError):
        stream.read()
    with pytest.raises(RuntimeError):
        stream.start()


def test_close_serializes_capture_reader_and_two_context_closers(context):
    stream = context._offline(device.CAPTURE)
    failures = []
    def read():
        try:
            for _ in range(5000):
                try:
                    stream.read(17)
                    stream.snapshot()
                except RuntimeError:
                    break
        except BaseException as error:
            failures.append(error)
    def close():
        try:
            context.close()
        except BaseException as error:
            failures.append(error)
    reader = threading.Thread(target=read)
    closers = [threading.Thread(target=close) for _ in range(2)]
    reader.start()
    for closer in closers:
        closer.start()
    for worker in [reader, *closers]:
        worker.join(5)
        assert not worker.is_alive()
    assert not failures and context.closed and stream.closed


def test_failed_open_never_starts_or_retains_candidate(context, monkeypatch):
    calls = []
    def failed(*arguments):
        calls.append(arguments[2])
        C.cast(arguments[-1], C.POINTER(C.c_int))[0] = -401
        return None
    monkeypatch.setattr(context.api, "bh_device_open_output", failed)
    with pytest.raises(OSError):
        context.create_output(1, endpoint_id="{0.0.0.00000000}.{00000000-0000-0000-0000-000000000000}")
    assert len(calls) == 1 and not context._streams
    for invalid in ("", "x"*64, "bad\0id", False):
        with pytest.raises(ValueError):
            context.create_capture(endpoint_id=invalid)


def test_module_does_not_import_python_audio_stack_and_stuck_os_control_cannot_hold_exit():
    code = '''
import sys, threading, json
from bluehead.audio_device import Context
c = Context()
entered = threading.Event()
def stuck():
    entered.set()
    threading.Event().wait(60)
threading.Thread(target=lambda: c._call(stuck), daemon=True).start()
assert entered.wait(3)
assert not any(name in sys.modules for name in ("av", "numpy", "sounddevice"))
print(json.dumps({"daemon": c._worker.daemon}), flush=True)
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["daemon"]

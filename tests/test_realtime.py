"""Native PCM boundary, quality and lifetime checks, entirely in memory."""
import ctypes
import threading
import time

import numpy as np
import pytest
import sounddevice as sd

from bluehead.audio import AudioOutput, Mixer
from bluehead.realtime import NativeCapture, NativeRender, MicrophoneStream, _owners
from .test_output_formats import audio_device


@pytest.mark.parametrize("rate", [8000, 16000, 44100, 48000, 88200, 96000, 192000, 384000])
def test_native_fallback_quality_channels_and_arbitrary_block_sizes(rate):
    mixer = Mixer(buffer_ms=0, max_ms=1000)
    mixer.add("tone")
    t = np.arange(48000) / 48000
    pcm = np.stack((.2*np.sin(2*np.pi*997*t), .1*np.sin(2*np.pi*1703*t)), axis=1).astype(np.float32)
    mixer.write("tone", pcm)
    renderer = NativeRender(mixer.native, rate)
    blocks, count = [], 0
    sizes = (1, 17, 113, 256, 511, 960)
    while count < rate // 2:
        size = min(sizes[len(blocks) % len(sizes)], rate // 2-count)
        out = np.empty((size, 2), np.float32)
        renderer.render_into(out)
        blocks.append(out)
        count += size
    output = np.concatenate(blocks)[rate//20:]
    assert np.isfinite(output).all()
    for channel, frequency, amplitude in ((0, 997, .2), (1, 1703, .1)):
        signal = output[:, channel]
        measured = np.sqrt(np.mean(signal**2))*np.sqrt(2)
        assert measured == pytest.approx(amplitude, rel=.01)
        peak = np.argmax(abs(np.fft.rfft(signal))) * rate / len(signal)
        assert abs(peak-frequency) < 3
    assert renderer.snapshot()["conversion_errors"] == 0
    renderer.close()


def test_downsampling_filters_out_of_band_audio():
    def level(frequency):
        c = NativeCapture(48000, 1, 16000)
        output, chunks = np.empty(1024, np.int16), []
        for i in range(100):
            pcm = (.5*np.sin(2*np.pi*frequency*np.arange(i*480, (i+1)*480)/48000)).astype(np.float32)
            c.push(pcm)
            while (count := c.read_into(output)):
                chunks.append(output[:count].copy())
        c.close()
        return np.sqrt(np.mean(np.concatenate(chunks)[1600:].astype(np.float64)**2))
    assert level(12000) / level(1000) < .0002  # >74 dB rejection, including S16 quantization.


@pytest.mark.parametrize("rate,channels,out_rate", [(48000, 1, 8000), (96000, 2, 16000),
                                                   (44100, 2, 16000), (48000, 1, 48000)])
def test_capture_stereo_downmix_rate_and_bounded_count(rate, channels, out_rate):
    capture = NativeCapture(rate, channels, out_rate)
    buffer, parts = np.empty(1024, np.int16), []
    for i in range(100):
        tone = (.2*np.sin(2*np.pi*1000*np.arange(i*(rate//100), (i+1)*(rate//100))/rate)).astype(np.float32)
        pcm = tone if channels == 1 else np.stack((tone, tone*.5), axis=1)
        assert capture.push(pcm) == len(pcm)
        while (count := capture.read_into(buffer)):
            parts.append(buffer[:count].copy())
    result = np.concatenate(parts)
    assert len(result) == out_rate
    signal = result[out_rate//10:].astype(np.float64)/32768
    assert np.std(signal) == pytest.approx(.2*(1 if channels == 1 else .75)/np.sqrt(2), rel=.01)
    assert capture.snapshot()["dropped_frames"] == 0
    capture.close()


def test_capture_overflow_discards_stale_audio_and_reset_and_halt():
    capture = NativeCapture(48000, 1, 48000)
    api, h = capture.api, capture.handle
    buffer = np.empty(4000, np.int16)
    assert capture.push(np.full(4000, .2, np.float32)) == 2880
    assert capture.snapshot()["dropped_frames"] == 1120
    assert capture.read_into(buffer) == 0
    capture.push(np.full(480, .4, np.float32))
    assert capture.read_into(buffer) == 480
    np.testing.assert_allclose(buffer[:480], .4*32768, atol=1)
    api.bh_capture_halt(h, 1)
    assert capture.push(np.ones(480, np.float32)) == 0
    api.bh_capture_reset(h)
    assert capture.snapshot()["queued_frames"] == 0
    api.bh_capture_halt(h, 0)
    api.bh_capture_push(h, None, 480)
    assert capture.read_into(buffer) == 480 and not buffer[:480].any()
    capture.close()


def test_native_capture_clips_nonfinite_values_and_rejects_unsafe_buffers():
    c = NativeCapture(48000, 1, 48000)
    c.push(np.array([0, 2, -2, np.nan, np.inf], np.float32))
    out = np.empty(5, np.int16)
    assert c.read_into(out) == 5
    np.testing.assert_array_equal(out, [0, 32767, -32768, 0, 0])
    for invalid in (np.empty((8, 2), np.float32), np.empty(8, np.float64), np.empty(8, np.float32)[::2]):
        with pytest.raises(ValueError):
            c.push(invalid)
    c.close()
    with pytest.raises(ValueError):
        c.read_into(out)


def test_capture_producer_consumer_wrap_is_finite_and_bounded():
    c = NativeCapture(48000, 2, 16000)
    pcm = np.full((137, 2), .25, np.float32)
    out = np.empty(1024, np.int16)
    def produce():
        for _ in range(2000):
            c.push(pcm)
    producer = threading.Thread(target=produce)
    producer.start()
    while producer.is_alive():
        count = c.read_into(out)
        assert np.isfinite(out[:count]).all()
        assert c.snapshot()["queued_frames"] <= 2880
    producer.join()
    c.close()


def test_native_callback_abi_stats_halt_master_and_invalid_sizes(monkeypatch):
    opened, _ = audio_device(monkeypatch, [96000])
    mixer = Mixer(buffer_ms=0)
    mixer.add("source")
    mixer.write("source", np.full((4800, 2), .2, np.float32))
    output = AudioOutput(mixer, 0)
    raw = opened[-1]
    out = np.empty((480, 2), np.float32)
    assert sd._ffi.typeof(raw.options["callback"]).kind == "function"
    assert raw.options["wrap_callback"] is None
    output.start()
    mixer.master = .5
    raw.render(out, 4)
    np.testing.assert_allclose(out, .1)
    assert output.underflows == 1 and output.stream.snapshot()["callbacks"] == 1
    output.stream.stop()
    queued = mixer.inputs["source"].frames
    raw.render(out)
    assert not out.any() and mixer.inputs["source"].frames == queued
    state = output.stream.state
    assert state.api.bh_output_callback(None, None, 0, None, 0, state.handle) == 0
    assert state.api.bh_output_callback(None, out.ctypes.data, 0xffffffff, None, 0, state.handle) == 2
    output.close()


def test_failed_close_keeps_userdata_owned_until_retry(monkeypatch):
    opened, _ = audio_device(monkeypatch, [48000])
    output = AudioOutput(Mixer(), 0)
    wrapper, raw = output.stream, opened[-1]
    original = raw.close
    raw.close = lambda **_: (_ for _ in ()).throw(RuntimeError("busy"))
    with pytest.raises(RuntimeError, match="busy"):
        output.close()
    assert wrapper in _owners and wrapper.state.handle and not wrapper.closed
    raw.close = original
    output.close()
    assert wrapper not in _owners and not wrapper.state.handle


def test_portaudio_close_failure_never_reuses_an_uncertain_handle(monkeypatch):
    opened, _ = audio_device(monkeypatch, [48000])
    output = AudioOutput(Mixer(), 0)
    wrapper, raw = output.stream, opened[-1]
    attempts = []
    def close(**_):
        attempts.append(True)
        raise wrapper.sd.PortAudioError("uncertain native close")
    raw.close = close
    try:
        with pytest.raises(wrapper.sd.PortAudioError):
            output.close()
        with pytest.raises(OSError, match="重新打开"):
            output.close()
        with pytest.raises(OSError):
            wrapper.start()
        assert len(attempts) == 1 and wrapper in _owners and wrapper.state.handle
    finally:
        # This is a fake stream: no real PortAudio handle or callback exists.
        # Only the test may safely simulate the process-exit cleanup.
        from bluehead import audio_runtime
        audio_runtime._retired.pop(id(wrapper), None)
        wrapper.state.close()
        wrapper.closed = True
        _owners.discard(wrapper)


def test_microphone_worker_drains_off_callback_and_cannot_route_after_stop(monkeypatch):
    opened, _ = audio_device(monkeypatch, [48000])
    written = []
    def write(data):
        assert threading.current_thread().name == "microphone-pcm"
        written.append(data)
    stream = MicrophoneStream(48000, 1, 16000, 0, write, lambda *a, **kw: None)
    stream.start()
    state, raw = stream.state, opened[-1]
    pcm = np.full(960, .2, np.float32)
    raw.options["callback"](sd._ffi.cast("void*", pcm.ctypes.data), sd._ffi.NULL, len(pcm),
                             sd._ffi.NULL, 0, raw.options["userdata"])
    deadline = time.monotonic()+2
    while not written and time.monotonic() < deadline:
        time.sleep(.002)
    assert written
    stream.stop()
    count = len(written)
    state.push(pcm)
    time.sleep(.01)
    assert len(written) == count
    stream.start()  # Endpoint rollback can restart a stopped worker.
    assert stream.active
    stream.close()
    assert not stream._thread.is_alive() and stream not in _owners


def test_invalid_context_rates_fail_without_allocation():
    mixer = Mixer()
    for rate in (0, 7999, 384001):
        with pytest.raises(ValueError):
            NativeRender(mixer.native, rate)
    with pytest.raises(ValueError):
        NativeCapture(48000, 3, 16000)
    assert ctypes.sizeof(ctypes.c_ulong) == 4 and ctypes.sizeof(ctypes.c_void_p) == 8


def test_telemetry_snapshot_cannot_race_native_state_release(monkeypatch):
    audio_device(monkeypatch, [48000])
    output = AudioOutput(Mixer(), 0)
    wrapper = output.stream
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    original = wrapper.state.snapshot
    result = []
    def snapshot():
        entered.set()
        assert release.wait(2)
        assert wrapper.state.handle, "Native state freed under an in-flight telemetry read"
        return original()
    monkeypatch.setattr(wrapper.state, "snapshot", snapshot)
    reader = threading.Thread(target=lambda: result.append(wrapper.snapshot()))
    reader.start()
    assert entered.wait(2)
    def close():
        output.close()
        closed.set()
    closer = threading.Thread(target=close)
    closer.start()
    try:
        assert not closed.wait(.02)
        assert wrapper.state.handle
    finally:
        release.set()
        reader.join(2)
        closer.join(2)
    assert closed.is_set() and result and not wrapper.state.handle
    assert wrapper.snapshot() == result[0]

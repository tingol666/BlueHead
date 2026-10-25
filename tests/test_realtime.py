"""Native PCM boundary, quality and lifetime checks, entirely in memory."""

import ctypes
import threading
import time

import numpy as np
import pytest

from bluehead.audio import AudioOutput, Mixer
from bluehead.realtime import NativeCapture, NativeRender
from .offline_audio import OfflineEndpoints


@pytest.mark.parametrize("rate", [8000, 16000, 44100, 48000, 88200, 96000, 192000, 384000])
def test_native_fallback_quality_channels_and_arbitrary_block_sizes(rate):
    mixer = Mixer(buffer_ms=0, max_ms=1000)
    mixer.add("tone")
    t = np.arange(48000) / 48000
    pcm = np.stack((0.2 * np.sin(2 * np.pi * 997 * t), 0.1 * np.sin(2 * np.pi * 1703 * t)), axis=1).astype(
        np.float32
    )
    mixer.write("tone", pcm)
    renderer = NativeRender(mixer.native, rate)
    blocks, count = [], 0
    sizes = (1, 17, 113, 256, 511, 960)
    while count < rate // 2:
        size = min(sizes[len(blocks) % len(sizes)], rate // 2 - count)
        out = np.empty((size, 2), np.float32)
        renderer.render_into(out)
        blocks.append(out)
        count += size
    output = np.concatenate(blocks)[rate // 20 :]
    assert np.isfinite(output).all()
    for channel, frequency, amplitude in ((0, 997, 0.2), (1, 1703, 0.1)):
        signal = output[:, channel]
        measured = np.sqrt(np.mean(signal**2)) * np.sqrt(2)
        assert measured == pytest.approx(amplitude, rel=0.01)
        peak = np.argmax(abs(np.fft.rfft(signal))) * rate / len(signal)
        assert abs(peak - frequency) < 3
    assert renderer.snapshot()["conversion_errors"] == 0
    renderer.close()


def test_downsampling_filters_out_of_band_audio():
    def level(frequency):
        c = NativeCapture(48000, 1, 16000)
        output, chunks = np.empty(1024, np.int16), []
        for i in range(100):
            pcm = (0.5 * np.sin(2 * np.pi * frequency * np.arange(i * 480, (i + 1) * 480) / 48000)).astype(
                np.float32
            )
            c.push(pcm)
            while count := c.read_into(output):
                chunks.append(output[:count].copy())
        c.close()
        return np.sqrt(np.mean(np.concatenate(chunks)[1600:].astype(np.float64) ** 2))

    assert level(12000) / level(1000) < 0.0002  # >74 dB rejection, including S16 quantization.


@pytest.mark.parametrize(
    "rate,channels,out_rate", [(48000, 1, 8000), (96000, 2, 16000), (44100, 2, 16000), (48000, 1, 48000)]
)
def test_capture_stereo_downmix_rate_and_bounded_count(rate, channels, out_rate):
    capture = NativeCapture(rate, channels, out_rate)
    buffer, parts = np.empty(1024, np.int16), []
    for i in range(100):
        tone = (
            0.2 * np.sin(2 * np.pi * 1000 * np.arange(i * (rate // 100), (i + 1) * (rate // 100)) / rate)
        ).astype(np.float32)
        pcm = tone if channels == 1 else np.stack((tone, tone * 0.5), axis=1)
        assert capture.push(pcm) == len(pcm)
        while count := capture.read_into(buffer):
            parts.append(buffer[:count].copy())
    result = np.concatenate(parts)
    assert len(result) == out_rate
    signal = result[out_rate // 10 :].astype(np.float64) / 32768
    assert np.std(signal) == pytest.approx(0.2 * (1 if channels == 1 else 0.75) / np.sqrt(2), rel=0.01)
    assert capture.snapshot()["dropped_frames"] == 0
    capture.close()


def test_capture_overflow_discards_stale_audio_and_reset_and_halt():
    capture = NativeCapture(48000, 1, 48000)
    api, h = capture.api, capture.handle
    buffer = np.empty(4000, np.int16)
    assert capture.push(np.full(4000, 0.2, np.float32)) == 2880
    assert capture.snapshot()["dropped_frames"] == 1120
    assert capture.read_into(buffer) == 0
    capture.push(np.full(480, 0.4, np.float32))
    assert capture.read_into(buffer) == 480
    np.testing.assert_allclose(buffer[:480], 0.4 * 32768, atol=1)
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
    pcm = np.full((137, 2), 0.25, np.float32)
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


def test_native_renderer_halt_master_and_unsafe_frame_sizes():
    mixer = Mixer(buffer_ms=0)
    mixer.add("source")
    mixer.write("source", np.full((4800, 2), 0.2, np.float32))
    renderer = NativeRender(mixer.native, 48000)
    out = np.empty((480, 2), np.float32)
    try:
        mixer.master = 0.5
        renderer.render_into(out)
        np.testing.assert_allclose(out, 0.1)
        assert renderer.snapshot()["conversion_errors"] == 0
        renderer.api.bh_render_halt(renderer.handle)
        queued = mixer.inputs["source"].frames
        renderer.render_into(out)
        assert not out.any() and mixer.inputs["source"].frames == queued
        assert renderer.api.bh_render_process(renderer.handle, out.ctypes.data, 0xFFFFFFFF) < 0
        renderer.api.bh_render_resume(renderer.handle)
        renderer.render_into(out)
        np.testing.assert_allclose(out, 0.1)
        for invalid in (np.empty((8, 1), np.float32), np.empty((8, 2), np.float64), out[::2]):
            with pytest.raises(ValueError):
                renderer.render_into(invalid)
    finally:
        renderer.close()


def test_failed_native_close_retains_stream_and_retries_without_freeing_state(monkeypatch):
    from bluehead import audio_runtime

    endpoints = OfflineEndpoints(monkeypatch)
    output = AudioOutput(Mixer(), 0)
    raw = output.stream
    original = endpoints.owner.api.bh_device_close
    attempts = []

    def close(handle):
        attempts.append(handle)
        return -77 if len(attempts) == 1 else original(handle)

    monkeypatch.setattr(endpoints.owner.api, "bh_device_close", close)
    try:
        with pytest.raises(OSError):
            output.close()
        assert raw.handle and not raw.closed and not output.closed
        assert audio_runtime._retired[id(raw)] is raw
        assert raw in endpoints.owner._streams
        assert raw.snapshot()["abi"] == 1
        output.close()
        assert raw.closed and not raw.handle and output.closed
        assert id(raw) not in audio_runtime._retired
        assert len(attempts) == 2
    finally:
        monkeypatch.setattr(endpoints.owner.api, "bh_device_close", original)
        output.close()
        endpoints.close()


def test_two_native_closers_release_exactly_one_handle(monkeypatch):
    endpoints = OfflineEndpoints(monkeypatch)
    raw = endpoints.create_capture()
    original = endpoints.owner.api.bh_device_close
    released = []

    def close(handle):
        released.append(handle)
        return original(handle)

    monkeypatch.setattr(endpoints.owner.api, "bh_device_close", close)
    failures = []

    def worker():
        try:
            raw.close()
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    assert not failures and len(released) == 1 and raw.closed and not raw.handle
    endpoints.close()


def test_microphone_native_queue_drains_off_callback_and_restarts_cleanly(monkeypatch):
    from bluehead.capture import CaptureStream

    endpoints = OfflineEndpoints(monkeypatch)
    written = []

    def write(data):
        assert threading.current_thread().name == "native-microphone-drain"
        written.append(data)

    stream = CaptureStream(0, 16000, write, lambda *args, **kwargs: None)
    try:
        stream.start()
        pcm = np.full(960, 0.2, np.float32)
        endpoints.process(stream.native, input=pcm)
        deadline = time.monotonic() + 2
        while not written and time.monotonic() < deadline:
            time.sleep(0.002)
        assert written and all(isinstance(data, bytes) for data in written)
        stream.stop()
        previous = len(written)
        time.sleep(0.01)
        assert len(written) == previous and not stream.active
        stream.start()
        assert stream.active
        endpoints.process(stream.native, input=np.full(960, 0.4, np.float32))
        deadline = time.monotonic() + 2
        while len(written) == previous and time.monotonic() < deadline:
            time.sleep(0.002)
        assert len(written) > previous
        assert np.mean(np.frombuffer(written[-1], dtype="<i2")[-100:]) > 0.38 * 32768
        stream.close()
        assert not stream.worker.is_alive() and stream.native.closed
        with pytest.raises(RuntimeError):
            stream.start()
    finally:
        stream.close()
        endpoints.close()


def test_invalid_context_rates_fail_without_allocation():
    mixer = Mixer()
    for rate in (0, 7999, 384001):
        with pytest.raises(ValueError):
            NativeRender(mixer.native, rate)
    with pytest.raises(ValueError):
        NativeCapture(48000, 3, 16000)
    assert ctypes.sizeof(ctypes.c_ulong) == 4 and ctypes.sizeof(ctypes.c_void_p) == 8


def test_telemetry_snapshot_serializes_native_state_release(monkeypatch):
    endpoints = OfflineEndpoints(monkeypatch)
    output = AudioOutput(Mixer(), 0)
    raw = output.stream
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    original = endpoints.owner.api.bh_device_stats
    result = []

    def stats(handle, record):
        entered.set()
        assert release.wait(2)
        assert raw.handle, "Native state freed under a telemetry read"
        return original(handle, record)

    monkeypatch.setattr(endpoints.owner.api, "bh_device_stats", stats)
    reader = threading.Thread(target=lambda: result.append(raw.snapshot()))
    reader.start()
    assert entered.wait(2)

    def close():
        output.close()
        closed.set()

    closer = threading.Thread(target=close)
    closer.start()
    try:
        assert not closed.wait(0.02)
        assert raw.handle
    finally:
        release.set()
        reader.join(2)
        closer.join(2)
    assert closed.is_set() and result and not raw.handle
    assert raw.snapshot()["frames"] == result[0]["frames"]
    assert raw.snapshot()["active"] == 0
    endpoints.close()

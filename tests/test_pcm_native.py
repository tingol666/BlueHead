import threading

import numpy as np
import pytest

from bluehead.audio import Mixer
from bluehead.calls import CallRouter


def test_native_mixer_matches_reference_under_mixing_and_peak_limiting():
    native = Mixer(buffer_ms=0)
    reference = Mixer(buffer_ms=0, native=False)
    assert native.native is not None
    generator = np.random.default_rng(3)
    for mixer in (native, reference):
        mixer.master = .9
        for key in ('phone', 'tablet', 'call:phone'):
            mixer.add(key)
        mixer.call_source, mixer.call_active = 'call:phone', True
        mixer.set_gain('tablet', .75)
    for _ in range(30):
        for key in ('phone', 'tablet', 'call:phone'):
            pcm = generator.uniform(-.8, .8, (480, 2)).astype(np.float32)
            for mixer in (native, reference):
                mixer.write(key, pcm)
        np.testing.assert_allclose(native.render(480), reference.render(480), atol=5e-7)


def test_call_prefills_in_thirty_ms_without_waiting_for_media_buffer():
    mixer = Mixer(buffer_ms=60, call_buffer_ms=30)
    mixer.master = 1
    mixer.add('call:phone')
    mixer.add('phone')
    pcm = np.full((1440, 2), .1, dtype=np.float32)
    mixer.write('call:phone', pcm)
    mixer.write('phone', pcm)
    mixer.call_source, mixer.call_active = 'call:phone', True
    np.testing.assert_allclose(mixer.render(480), .1, atol=1e-6)
    assert mixer.inputs['phone'].frames == 1440
    assert mixer.snapshot()['call_buffer_ms'] == 30


def test_capture_backlog_is_bounded_by_time_and_drops_stale_speech():
    router = CallRouter()
    router.select('phone')
    router.configure(16000)
    router.write_microphone('phone', b'a' * 2000)
    router.write_microphone('phone', b'b' * 2000)
    assert len(router.buffer) == 1920
    assert router.read('phone', 1920) == b'b' * 1920


def test_native_render_and_producer_concurrency_is_bounded_and_finite():
    mixer = Mixer(buffer_ms=0)
    mixer.add('phone')
    pcm = np.full((480, 2), .1, np.float32)
    def produce():
        for _ in range(400):
            mixer.write('phone', pcm)
    producer = threading.Thread(target=produce)
    producer.start()
    output = np.empty((480, 2), np.float32)
    for _ in range(400):
        mixer.render_into(output)
        assert np.isfinite(output).all() and np.max(abs(output)) <= .98
    producer.join(2)
    assert not producer.is_alive()
    assert mixer.inputs['phone'].frames <= mixer.maximum


def test_clock_drift_compensation_preserves_high_frequency_level_and_queue():
    mixer = Mixer(buffer_ms=60)
    mixer.master = 1
    mixer.add('phone')
    source_rate = 48000 * 1.00025
    generated, due = 0, 2880.0
    levels = []
    # 45 seconds of 250 ppm source-clock mismatch, simulated without sleeping.
    for block in range(4500):
        due += source_rate / 100
        count = int(due) - generated
        tone = .2 * np.sin(2*np.pi*20000*np.arange(generated, generated+count)/source_rate)
        mixer.write('phone', np.repeat(tone[:, None], 2, axis=1).astype(np.float32))
        generated += count
        output = mixer.render(480)
        if block > 4000:
            levels.append(np.sqrt(np.mean(output[:, 0] ** 2)))
    np.testing.assert_allclose(levels, .2 / np.sqrt(2), rtol=.025)
    assert mixer.inputs['phone'].dropped == 0
    assert 20 < mixer.inputs['phone'].frames / 48 < 100


def test_native_rejects_unsafe_buffers_before_entering_c():
    mixer = Mixer(buffer_ms=0)
    for buffer in (np.empty((480, 1), np.float32), np.empty((480, 2), np.float64),
                   np.empty((480, 4), np.float32)[:, ::2]):
        with pytest.raises(ValueError, match="PCM output"):
            mixer.native.render_into(buffer, 1)
    stream = mixer.native.add(0, 4800)
    with pytest.raises(ValueError, match="PCM input"):
        stream.write(np.empty((480, 1), np.float32))

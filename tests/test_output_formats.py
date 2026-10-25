"""Real native output callbacks behind virtual WASAPI endpoint selection."""

import numpy as np
import pytest

from bluehead.audio import AudioOutput, Mixer
from bluehead.audio_device import PLAYBACK
from .offline_audio import OfflineEndpoints


@pytest.mark.parametrize("native_rate", [44100, 96000])
def test_shared_wasapi_path_keeps_one_48khz_pcm_timeline(monkeypatch, native_rate):
    endpoints = OfflineEndpoints(monkeypatch, [native_rate])
    mixer = Mixer(buffer_ms=0)
    mixer.add("source")
    mixer.write("source", np.full((4800, 2), 0.125, np.float32))
    monkeypatch.setattr(mixer, "render", lambda _: pytest.fail("Python audio callback"))
    monkeypatch.setattr(mixer, "render_into", lambda _: pytest.fail("Python audio callback"))
    output = AudioOutput(mixer, 0)
    try:
        output.start()
        raw = endpoints.opened[-1]
        assert raw.snapshot()["sample_rate"] == 48000
        assert raw.snapshot()["conversion_mode"] == "Windows shared SRC"
        block = np.empty((480, 2), np.float32)
        endpoints.process(raw, output=block)
        np.testing.assert_array_equal(block, 0.125)
        assert raw.snapshot()["callbacks"] == 1
        assert mixer.inputs["source"].frames == 4320
    finally:
        output.close()
        endpoints.close()


@pytest.mark.parametrize("native_rate", [44100, 96000])
def test_native_format_fallback_uses_real_speex_conversion(monkeypatch, native_rate):
    endpoints = OfflineEndpoints(monkeypatch, [native_rate], shared=False)
    mixer = Mixer(buffer_ms=0)
    mixer.add("source")
    mixer.write("source", np.full((6000, 2), 0.1, np.float32))
    output = AudioOutput(mixer, 0)
    try:
        output.start()
        raw = endpoints.opened[-1]
        assert raw.snapshot()["sample_rate"] == native_rate
        assert raw.snapshot()["conversion_mode"] == "SpeexDSP fallback"
        block = np.empty((native_rate // 10, 2), np.float32)
        endpoints.process(raw, output=block)
        assert np.isfinite(block).all()
        np.testing.assert_allclose(block[native_rate // 50 :], 0.1, atol=2e-5)
        assert raw.snapshot()["conversion_errors"] == 0
        assert 0 < mixer.inputs["source"].frames < 1201
    finally:
        output.close()
        endpoints.close()


def test_hot_switch_between_mix_formats_preserves_exactly_one_consumer(monkeypatch):
    endpoints = OfflineEndpoints(monkeypatch, [96000, 44100])
    mixer = Mixer(buffer_ms=0)
    mixer.add("call:source")
    mixer.write("call:source", np.full((3000, 2), 0.2, np.float32))
    output = AudioOutput(mixer, 0)
    output.start()
    try:
        old = output.stream
        output.switch_device(1)
        current = output.stream
        assert old.closed and endpoints.active[PLAYBACK] == {current}
        block = np.empty((480, 2), np.float32)
        endpoints.process(current, output=block)
        np.testing.assert_allclose(block, 0.2)
        assert mixer.inputs["call:source"].frames == 2520
        assert all(s.snapshot()["sample_rate"] == 48000 for s in endpoints.opened)
    finally:
        output.close()
        endpoints.close()
    assert not endpoints.active[PLAYBACK]


@pytest.mark.parametrize("failure", ["open", "start"])
def test_failed_native_replacement_rolls_back_without_losing_mixer_pcm(monkeypatch, failure):
    endpoints = OfflineEndpoints(monkeypatch, [48000, 96000])
    mixer = Mixer(buffer_ms=0)
    mixer.add("source")
    mixer.write("source", np.full((3000, 2), 0.15, np.float32))
    output = AudioOutput(mixer, 0)
    output.start()
    try:
        old = output.stream
        getattr(endpoints, "fail_" + failure).add(1)
        with pytest.raises(OSError, match="endpoint"):
            output.switch_device(1)
        assert output.stream is old and not old.closed and old.active
        assert endpoints.active[PLAYBACK] == {old}
        assert mixer.inputs["source"].frames == 3000
        if failure == "start":
            assert endpoints.opened[-1].closed
        block = np.empty((480, 2), np.float32)
        endpoints.process(old, output=block)
        np.testing.assert_allclose(block, 0.15)
    finally:
        output.close()
        endpoints.close()

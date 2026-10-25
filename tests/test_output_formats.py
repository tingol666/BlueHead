"""The real C callback with fake PortAudio endpoints: no physical devices opened."""
from types import SimpleNamespace
import sys

import numpy as np
import pytest
import sounddevice as real_sd

from bluehead.audio import AudioOutput, Mixer


def audio_device(monkeypatch, rates, *, wasapi=True, reject=None):
    opened, active = [], set()

    class AudioError(Exception):
        pass

    def info(device=None, direction=None):
        return {"hostapi": 0, "default_samplerate": rates[device or 0]}

    def check(**options):
        if reject == "probe" and options["extra_settings"]:
            raise AudioError("Driver does not support automatic conversion")
        if not wasapi and options["samplerate"] != rates[options["device"]]:
            raise AudioError("Native sample rate required")

    class Stream:
        def __init__(self, **options):
            if options["extra_settings"] and reject == "open":
                raise AudioError("Format probe passed, but stream creation failed")
            self.options = options
            self.device = options["device"]
            self.active = self.closed = False
            opened.append(self)

        def start(self):
            assert not active, "Two output callbacks could drain the same mixer"
            self.active = True
            active.add(self)

        def stop(self, ignore_errors=False):
            self.active = False
            active.discard(self)

        def close(self, ignore_errors=False):
            self.stop()
            self.closed = True

        def render(self, block, status=0):
            ffi = real_sd._ffi
            result = self.options["callback"](ffi.NULL, ffi.cast("void*", block.ctypes.data), len(block),
                                               ffi.NULL, status, self.options["userdata"])
            assert result == 0

    api = {"name": "Windows WASAPI" if wasapi else "Other API", "default_output_device": 0}
    sd = SimpleNamespace(query_devices=info, query_hostapis=lambda: [api],
                         WasapiSettings=lambda **options: options, check_output_settings=check,
                         _StreamBase=Stream, _ffi=real_sd._ffi, PortAudioError=AudioError)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    return opened, active


@pytest.mark.parametrize("native_rate", [44100, 96000])
def test_wasapi_uses_native_pcm_callback_at_non_48khz_endpoint(monkeypatch, native_rate):
    opened, _ = audio_device(monkeypatch, [native_rate])

    mixer = Mixer(buffer_ms=0)
    mixer.add("source")
    mixer.write("source", np.full((4800, 2), .125, np.float32))
    monkeypatch.setattr(mixer, "render", lambda _: pytest.fail("Python callback"))
    monkeypatch.setattr(mixer, "render_into", lambda _: pytest.fail("Python callback"))
    output = AudioOutput(mixer, 0)
    try:
        stream = opened[-1]
        assert stream.options["samplerate"] == 48000
        assert stream.options["extra_settings"] == {"auto_convert": True}
        block = np.empty((480, 2), np.float32)
        stream.render(block)
        np.testing.assert_array_equal(block, .125)
    finally:
        output.close()


@pytest.mark.parametrize("reject", ["probe", "open"])
def test_wasapi_driver_rejection_falls_back_to_native_format(monkeypatch, reject):
    opened, _ = audio_device(monkeypatch, [96000], reject=reject)
    mixer = Mixer(buffer_ms=0)
    mixer.add("source")
    mixer.write("source", np.full((3000, 2), .1, np.float32))
    output = AudioOutput(mixer, 0)
    try:
        stream = opened[-1]
        assert stream.options["samplerate"] == 96000
        assert stream.options["extra_settings"] is None
        block = np.empty((960, 2), np.float32)
        stream.render(block)
        assert np.isfinite(block).all() and np.mean(block) > .09
    finally:
        output.close()


def test_non_wasapi_device_keeps_existing_native_format_path(monkeypatch):
    opened, _ = audio_device(monkeypatch, [44100], wasapi=False)
    output = AudioOutput(Mixer(buffer_ms=0), 0)
    try:
        assert opened[-1].options["samplerate"] == 44100
        assert opened[-1].options["extra_settings"] is None
    finally:
        output.close()


def test_hot_switch_between_different_mix_formats_keeps_one_48khz_consumer(monkeypatch):
    opened, active = audio_device(monkeypatch, [96000, 44100])
    mixer = Mixer(buffer_ms=0)
    mixer.add("call:source")
    mixer.write("call:source", np.full((3000, 2), .2, np.float32))
    output = AudioOutput(mixer, 0)
    output.start()
    try:
        output.switch_device(1)
        assert opened[0].closed and active == {opened[1]}
        assert all(s.options["samplerate"] == 48000 for s in opened)
        assert all(s.options["extra_settings"] == {"auto_convert": True} for s in opened)
        block = np.empty((480, 2), np.float32)
        opened[1].render(block)
        np.testing.assert_allclose(block, .2)
        assert mixer.inputs["call:source"].frames == 2520
    finally:
        output.close()
    assert not active

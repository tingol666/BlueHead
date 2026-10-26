"""Actual native stereo PCM concealment, recovery and clock matching."""
import ctypes as C
import math

import numpy as np
import pytest

from bluehead.media import NativeMedia
from bluehead.protocol import Format
from .test_uplink_clock import _core
from .test_native_runtime import packet


def core():
    api = _core()
    api.bh_mix_continuity.argtypes = [C.c_void_p, C.c_int, C.c_int, C.c_uint, C.c_uint]
    api.bh_mix_continuity.restype = C.c_int
    return api


def wave(first, count, frequency=1000):
    left = (.2 * np.sin(np.arange(first, first + count) * 2 * np.pi * frequency / 48000)).astype("f4")
    # Antiphase channels expose independently chosen stereo timing.
    return np.column_stack((left, -left)).astype("f4")


def render(api, mixer, count=480):
    output = np.empty((count, 2), "f4")
    assert api.bh_mix_render(mixer, output.ctypes.data, count, 1) == 1
    assert np.isfinite(output).all()
    return output


def test_short_tail_loss_is_concealed_without_full_prebuffer_and_stereo_stays_linked():
    api = core()
    mixer = api.bh_mix_create()
    source = api.bh_mix_add(mixer, 0, 8640)
    assert api.bh_mix_continuity(mixer, source, 1, 0, 0) == 0
    try:
        data = wave(0, 2400)
        api.bh_mix_write(mixer, source, data.ctypes.data, len(data))
        for _ in range(5):
            render(api, mixer)
        recovered = render(api, mixer, 240)
        assert np.sqrt(np.mean(recovered**2)) > .12
        assert api.bh_mix_value(mixer, source, 10) == 0
        assert api.bh_mix_value(mixer, source, 7) == 240
        np.testing.assert_allclose(recovered[:, 0], -recovered[:, 1], atol=1e-7)
        data = wave(2640, 960)
        api.bh_mix_write(mixer, source, data.ctypes.data, len(data))
        real = render(api, mixer)
        assert np.max(np.abs(np.diff(np.r_[recovered[-1, 0], real[:, 0]]))) < .05
        assert api.bh_mix_value(mixer, source, 9) == 192
    finally:
        api.bh_mix_free(mixer)


def test_long_outage_fades_and_enters_rebuffer_once_then_recovers():
    api = core()
    mixer = api.bh_mix_create()
    source = api.bh_mix_add(mixer, 960, 8640)
    api.bh_mix_continuity(mixer, source, 1, 960, 2880)
    try:
        data = wave(0, 2400)
        api.bh_mix_write(mixer, source, data.ctypes.data, len(data))
        output = [render(api, mixer) for _ in range(20)]
        assert api.bh_mix_value(mixer, source, 10) == 1
        assert not np.any(output[-1])
        assert api.bh_mix_value(mixer, source, 7) <= 1440
        data = wave(2400, 2880)
        api.bh_mix_write(mixer, source, data.ctypes.data, len(data))
        resumed = render(api, mixer)
        assert np.sqrt(np.mean(resumed**2)) > .08
        assert abs(resumed[0, 0]) < .002
    finally:
        api.bh_mix_free(mixer)


def test_continual_partial_supply_cannot_conceal_forever():
    api = core()
    mixer = api.bh_mix_create()
    source = api.bh_mix_add(mixer, 0, 8640)
    api.bh_mix_continuity(mixer, source, 1, 0, 0)
    try:
        for i in range(30):
            data = wave(i * 400, 400)
            api.bh_mix_write(mixer, source, data.ctypes.data, len(data))
            render(api, mixer)
        assert api.bh_mix_value(mixer, source, 10) > 0
    finally:
        api.bh_mix_free(mixer)


def test_rtp_missing_frames_are_separate_from_real_decode_count_and_ssrc_resets():
    decoder = NativeMedia(Format("sbc", 48000, 2))
    decoder.api.bh_media_continuity.argtypes = [C.c_void_p, C.c_int]
    decoder.api.bh_media_continuity(decoder.handle, 1)
    try:
        produced = 0
        for i in range(100):
            if i == 50:
                continue
            decoder.decode_packet(packet(i))
            produced += len(decoder.read_pcm()) // 8
        assert decoder.value(10) == 128
        assert produced == decoder.frames + 128
        prior = decoder.frames
        for i in range(30):
            p = bytearray(packet(i))
            p[8:12] = b"new!"
            decoder.decode_packet(bytes(p))
            decoder.read_pcm()
        assert decoder.frames > prior + 3000
        assert decoder.value(11) >= 1
    finally:
        decoder.close()


def test_native_rtp_timestamp_and_sequence_wrap_keep_a_continuous_timeline():
    import struct
    decoder = NativeMedia(Format("sbc", 48000, 2))
    decoder.api.bh_media_continuity.argtypes = [C.c_void_p, C.c_int]
    decoder.api.bh_media_continuity(decoder.handle, 1)
    try:
        for i in range(100):
            data = bytearray(packet(i))
            struct.pack_into(">HI", data, 2, (65500 + i) & 65535, (0xfffff000 + i * 128) & 0xffffffff)
            decoder.decode_packet(bytes(data))
            decoder.read_pcm()
        assert decoder.frames > 12000
        assert decoder.lost == decoder.value(10) == decoder.value(11) == 0
    finally:
        decoder.close()


@pytest.mark.parametrize("ppm", [-500, 500])
def test_native_continuity_clock_with_stereo_tone_10_virtual_minutes(ppm):
    api = core()
    mixer = api.bh_mix_create()
    source = api.bh_mix_add(mixer, 2880, 8640)
    api.bh_mix_continuity(mixer, source, 1, 1440, 5760)
    pieces, packet_index, minimum, maximum = [], 1, 100000, 0
    data = wave(0, 480)
    try:
        for cycle in range(60000):
            now = cycle * .01
            while packet_index * .01 / (1 + ppm / 1e6) + .003 * math.sin(packet_index * 1.113) <= now:
                api.bh_mix_write(mixer, source, data.ctypes.data, 480)
                packet_index += 1
            pcm = render(api, mixer)
            if cycle > 50000:
                q = api.bh_mix_value(mixer, source, 0)
                minimum, maximum = min(minimum, q), max(maximum, q)
            if cycle > 59000:
                pieces.append(pcm)
        assert api.bh_mix_value(mixer, source, 1) == 0
        assert api.bh_mix_value(mixer, source, 3) == 0
        assert 0 < minimum <= maximum < 4800
        assert abs(int(api.bh_mix_value(mixer, source, 2)) - 1000 - ppm) < 70
        signal = np.concatenate(pieces)
        np.testing.assert_allclose(signal[:, 0], -signal[:, 1], atol=1e-7)
        assert np.sqrt(np.mean(signal**2)) == pytest.approx(.2 / np.sqrt(2), rel=.03)
        spectrum = np.abs(np.fft.rfft(signal[:, 0]))
        measured = np.argmax(spectrum) * 48000 / len(signal)
        assert abs(measured - 1000) < 1
    finally:
        api.bh_mix_free(mixer)

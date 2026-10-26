"""Actual native stereo PCM concealment, recovery and clock matching."""
import ctypes as C
import base64
import json
import math
from pathlib import Path
import struct

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


@pytest.mark.parametrize('codec,packet_frames,missing_packets', [('aac', 1024, 1),
                                                               ('aptx_hd', 1024, 1),
                                                               ('sbc', 128, 10)])
def test_rtp_loss_keeps_complete_time_even_beyond_waveform_prediction_budget(codec, packet_frames, missing_packets):
    vectors = json.loads((Path(__file__).parents[1] / 'bluehead/assets/selfcheck_vectors.json').read_text('utf-8'))
    vector = next(v for v in vectors['vectors'] if v['codec'] == codec)
    decoder = NativeMedia(Format(codec, 48000, 2))
    decoder.api.bh_media_continuity.argtypes = [C.c_void_p, C.c_int]
    decoder.api.bh_media_continuity(decoder.handle, 1)
    produced = 0
    try:
        for sequence in range(120):
            if 30 <= sequence < 30 + missing_packets:
                continue
            data = bytearray(base64.b64decode(vector['packets'][sequence % len(vector['packets'])]))
            struct.pack_into('>HI', data, 2, sequence, sequence * packet_frames)
            decoder.decode_packet(bytes(data))
            produced += len(decoder.read_pcm()) // 8
        # A 21.33ms AAC/aptX HD packet is not a stream reset. Beyond 20ms
        # prediction may be silent, but it must still occupy its actual time.
        assert decoder.value(10) == missing_packets * packet_frames
        assert decoder.value(11) == 0
        assert produced == decoder.frames + missing_packets * packet_frames
    finally:
        decoder.close()


def test_aac_repeated_packet_loss_does_not_accumulate_clock_debt_over_two_virtual_minutes():
    vectors = json.loads((Path(__file__).parents[1] / 'bluehead/assets/selfcheck_vectors.json').read_text('utf-8'))
    packets = next(v['packets'] for v in vectors['vectors'] if v['codec'] == 'aac')
    api = core()
    api.bh_mix_test_write_at.argtypes = [C.c_void_p, C.c_int, C.c_void_p, C.c_uint, C.c_ulonglong]
    frequency = C.c_longlong()
    C.windll.kernel32.QueryPerformanceFrequency(C.byref(frequency))
    mixer = api.bh_mix_create()
    source = api.bh_mix_add(mixer, 2880, 8640)
    api.bh_mix_continuity(mixer, source, 1, 1440, 5760)
    decoder = NativeMedia(Format('aac', 48000, 2))
    decoder.api.bh_media_continuity.argtypes = [C.c_void_p, C.c_int]
    decoder.api.bh_media_continuity(decoder.handle, 1)
    packet_index, lost = 0, 0
    try:
        for block in range(12000):
            now = block * .01
            while (arrival := packet_index * 1024 / (48000 * .9995) +
                   .003 * math.sin(packet_index * 1.113)) <= now:
                if packet_index and packet_index % 31 == 0:
                    lost += 1
                else:
                    data = bytearray(base64.b64decode(packets[packet_index % len(packets)]))
                    struct.pack_into('>HI', data, 2, packet_index & 65535, packet_index * 1024)
                    decoder.decode_packet(bytes(data))
                    pcm = decoder.read_pcm()
                    if pcm:
                        api.bh_mix_test_write_at(mixer, source, pcm, len(pcm) // 8,
                                               1000000 + int(arrival * frequency.value))
                packet_index += 1
            render(api, mixer)
        assert lost > 100
        assert decoder.value(10) == lost * 1024
        assert api.bh_mix_value(mixer, source, 1) == 0
        assert api.bh_mix_value(mixer, source, 3) == 0
    finally:
        decoder.close()
        api.bh_mix_free(mixer)


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


def test_jitter_retarget_preserves_a_continuous_sample_timeline_without_missing_audio():
    # A ramp reveals temporal seams hidden by a periodic sine-only check.
    # The other channel carries a real tone. No packet or sample is missing.
    api = core()
    api.bh_mix_test_write_at.argtypes = [C.c_void_p, C.c_int, C.c_void_p, C.c_uint, C.c_ulonglong]
    frequency = C.c_longlong()
    C.windll.kernel32.QueryPerformanceFrequency(C.byref(frequency))
    mixer = api.bh_mix_create()
    source = api.bh_mix_add(mixer, 2880, 8640)
    api.bh_mix_continuity(mixer, source, 1, 1440, 5760)
    pieces, packet_index = [], 1
    try:
        for block in range(3000):
            now = block * .01
            while packet_index * .01 + .003 * math.sin(packet_index * 1.113) <= now:
                frame = np.arange((packet_index - 1) * 480, packet_index * 480)
                data = np.column_stack((frame * 1e-7, .12 * np.sin(frame * 2 * np.pi * 1000 / 48000))).astype('f4')
                arrival = packet_index * .01 + .003 * math.sin(packet_index * 1.113)
                api.bh_mix_test_write_at(mixer, source, data.ctypes.data, 480,
                                       1000000 + int(arrival * frequency.value))
                packet_index += 1
            pieces.append(render(api, mixer))
        audio = np.concatenate(pieces)
        signal = audio[3 * 48000:28 * 48000, 0]
        # A short window separates temporal seams from float32 filter rounding.
        advance = (signal[32:] - signal[:-32]) / (32e-7)
        assert advance.min() > .8
        assert advance.max() < 1.2
        assert api.bh_mix_value(mixer, source, 1) == 0
        assert api.bh_mix_value(mixer, source, 3) == 0
    finally:
        api.bh_mix_free(mixer)

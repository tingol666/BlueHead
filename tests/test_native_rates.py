"""Native source/output clocks: precision, alias rejection and route lifetime."""
import ctypes as C
import struct
import time
from types import SimpleNamespace

import numpy as np
import pytest

from bluehead.realtime import NativeRender
from bluehead.native_runtime import NativeClient
from tools.audio_fidelity_audit import ROOT, mixer_api
from .vectors import LdacEncoder, rtp, signal


def create(rate, pcm):
    api = mixer_api(ROOT / 'bluehead/assets/bluehead_audio.dll')
    api.bh_mix_format.argtypes = [C.c_void_p, C.c_int, C.c_uint]
    api.bh_mix_value.argtypes = [C.c_void_p, C.c_int, C.c_int]
    api.bh_mix_value.restype = C.c_uint64
    mixer = api.bh_mix_create()
    source = add(api, mixer, rate, pcm)
    return api, mixer, source


def add(api, mixer, rate, pcm):
    source = api.bh_mix_add(mixer, 0, 48000)
    assert source >= 0
    assert api.bh_mix_format(mixer, source, rate) == 0
    assert api.bh_mix_continuity(mixer, source, 1, 0, 0) == 0
    assert api.bh_mix_timeline(mixer, source, 1) == 0
    api.bh_mix_write(mixer, source, pcm.ctypes.data, len(pcm))
    return source


def render(renderer, count, sizes=(1, 17, 113, 511, 960)):
    output = np.empty((count, 2), 'f4')
    offset = block = 0
    while offset < count:
        n = min(sizes[block % len(sizes)], count - offset)
        renderer.render_into(output[offset:offset+n])
        offset += n
        block += 1
    return output


@pytest.mark.parametrize('rate', [8000, 16000, 44100, 48000, 96000, 192000, 384000])
def test_native_rate_unity_retains_decoder_pcm_exactly(rate):
    rng = np.random.default_rng(212)
    pcm = (rng.integers(-2**21, 2**21, (rate // 8, 2)) / 2**23).astype('f4')
    pcm[100:200] *= np.float32(2**-14)
    api, mixer, source = create(rate, pcm)
    renderer = NativeRender(SimpleNamespace(handle=mixer), rate)
    try:
        output = render(renderer, rate // 10)
        delay = (rate * 3 + 1999) // 2000
        np.testing.assert_array_equal(output[delay:], pcm[:len(output)-delay])
        assert api.bh_mix_value(mixer, source, 3) == 0
        assert api.bh_mix_value(mixer, source, 20) == rate
    finally:
        renderer.close()
        api.bh_mix_free(mixer)


def test_isolated_ldac_worker_publishes_96k_pcm_on_actual_native_hci_route():
    encoder = LdacEncoder(96000, 0)
    try:
        payloads = encoder.encode(signal(96000, .12))
    finally:
        encoder.close()
    with NativeClient(spawn=True) as client:
        client.request('mix_add', key='native96')
        client.request('media_config', key='native96', codec=5, rate=96000, channels=2)
        client.request('media_route', key='native96', handle=1, cid=64)
        client.request('media_pause', key='native96', paused=False)
        frames = 0
        for sequence, payload in enumerate(payloads):
            packet = rtp(payload, sequence, frames)
            acl = struct.pack('<HHHH', 1, len(packet)+4, len(packet), 64) + packet
            client.request('test_hci_acl', packet=acl.hex())
            frames += payload[0] * 256
            end = time.monotonic() + 1
            while time.monotonic() < end:
                stats = client.request('media_snapshot', key='native96')
                if stats['decoded_frames'] == frames:
                    break
                time.sleep(.001)
            assert stats['decoded_frames'] == frames
        assert frames > 9000
        assert stats['decode_errors'] == stats['dropped_packets'] == 0
        assert client.request('snapshot')['sources']['native96']['pcm_rate'] == 96000
        assert client.request('test_render', frames=480)['rms'] > .01


def test_low_rate_packet_cadence_with_zero_user_target_does_not_starve_forever():
    api, mixer, source = create(8000, np.empty((0, 2), 'f4'))
    renderer = NativeRender(SimpleNamespace(handle=mixer), 96000)
    heard = False
    try:
        for frame in range(100):
            t = (np.arange(60) + frame*60) / 8000
            pcm = np.column_stack((.1*np.sin(2*np.pi*997*t), .05*np.cos(2*np.pi*997*t))).astype('f4')
            api.bh_mix_write(mixer, source, pcm.ctypes.data, len(pcm))
            output = render(renderer, 720, (720,))
            if frame >= 4:
                assert np.max(abs(output)) > .03
                heard = True
        assert heard and api.bh_mix_value(mixer, source, 3) == 0
        assert api.bh_mix_value(mixer, source, 17) == 0
    finally:
        renderer.close()
        api.bh_mix_free(mixer)


def test_mix_preserves_low_level_detail_during_cancellation_before_final_float_rounding():
    api, mixer, _ = create(48000, np.full((2000, 2), .75, 'f4'))
    for level in (2**-25, -.75, .25, -2**-26, -.25, 0, 0):
        add(api, mixer, 48000, np.full((2000, 2), level, 'f4'))
    renderer = NativeRender(SimpleNamespace(handle=mixer), 48000)
    try:
        output = render(renderer, 1800)
        np.testing.assert_array_equal(output[72:], np.full_like(output[72:], 2**-26))
    finally:
        renderer.close()
        api.bh_mix_free(mixer)


@pytest.mark.parametrize('source_rate,output_rate', [(96000, 48000), (192000, 48000), (44100, 96000), (96000, 192000), (48000, 8000), (48000, 16000), (96000, 32000)])
def test_one_pass_src_retains_passband_and_rejects_aliases(source_rate, output_rate):
    t = np.arange(source_rate // 3) / source_rate
    tones = [997, min(20003, min(source_rate, output_rate) * .4375)]
    if source_rate > output_rate:
        tones.append(output_rate * .505)
    mono = sum(.1 * np.sin(2*np.pi*f*t + .37) for f in tones)
    pcm = np.column_stack((mono, -.31*mono)).astype('f4')
    api, mixer, _ = create(source_rate, pcm)
    renderer = NativeRender(SimpleNamespace(handle=mixer), output_rate)
    try:
        output = render(renderer, output_rate // 4)[output_rate // 20:]
        t = (np.arange(output_rate // 20, output_rate // 4) - (output_rate*3+1999)//2000) / output_rate
        expected = sum(.1 * np.sin(2*np.pi*f*t + .37) for f in tones[:2])
        assert np.max(abs(output[:, 0] - expected)) < 4e-7
        np.testing.assert_allclose(output[:, 1], -.31*output[:, 0], atol=3e-8)
    finally:
        renderer.close()
        api.bh_mix_free(mixer)


def test_mixed_native_sources_and_96k_ultrasonic_band_do_not_pass_through_48k():
    t96, t44 = np.arange(24000) / 96000, np.arange(11025) / 44100
    a = np.column_stack((.1*np.sin(2*np.pi*40009*t96), np.zeros(len(t96)))).astype('f4')
    b = np.column_stack((np.zeros(len(t44)), .1*np.sin(2*np.pi*13001*t44))).astype('f4')
    api, mixer, _ = create(96000, a)
    add(api, mixer, 44100, b)
    renderer = NativeRender(SimpleNamespace(handle=mixer), 96000)
    try:
        output = render(renderer, 16000)
        t = (np.arange(2000, 16000) - 144) / 96000
        np.testing.assert_allclose(output[2000:, 0], .1*np.sin(2*np.pi*40009*t), atol=1e-8)
        np.testing.assert_allclose(output[2000:, 1], .1*np.sin(2*np.pi*13001*t), atol=1e-7)
    finally:
        renderer.close()
        api.bh_mix_free(mixer)


def test_endpoint_rate_change_keeps_native_source_cursor_and_preparing_does_not_consume():
    t = np.arange(24000) / 96000
    pcm = np.column_stack((.1*np.sin(2*np.pi*997*t), .07*np.cos(2*np.pi*1703*t))).astype('f4')
    api, mixer, source = create(96000, pcm)
    old = NativeRender(SimpleNamespace(handle=mixer), 96000)
    new = None
    try:
        render(old, 4800)
        queued = api.bh_mix_value(mixer, source, 0)
        new = NativeRender(SimpleNamespace(handle=mixer), 48000)
        assert api.bh_mix_value(mixer, source, 0) == queued
        old.close()  # The endpoint actor stops/joins the old consumer before commit.
        old = None
        output = render(new, 6000)
        t = (4800 + (np.arange(500, 6000) - 72) * 2) / 96000
        np.testing.assert_allclose(output[500:, 0], .1*np.sin(2*np.pi*997*t), atol=1e-7)
        np.testing.assert_allclose(output[500:, 1], .07*np.cos(2*np.pi*1703*t), atol=1e-7)
        assert api.bh_mix_value(mixer, source, 3) == 0
    finally:
        if old:
            old.close()
        if new:
            new.close()
        api.bh_mix_free(mixer)

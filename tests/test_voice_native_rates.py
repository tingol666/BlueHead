"""Codec-native PCM contracts and exact integer-boundary references."""
import ctypes as C
import time

import av
import numpy as np
import pytest
from bluehead.native_runtime import NativeClient

from .test_native_voice import dll as dll  # Reuse the pinned native library fixture.


@pytest.fixture
def api(dll):
    dll.bh_voice_create_at.argtypes = [C.c_uint32, C.c_uint32]
    dll.bh_voice_create_at.restype = C.c_void_p
    dll.bh_voice_pcm_rate.argtypes = [C.c_void_p]
    dll.bh_voice_pcm_rate.restype = C.c_uint32
    dll.bh_voice_encode_f32.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32]
    dll.bh_voice_read_f32.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32]
    dll.bh_voice_read_f32.restype = C.c_uint32
    return dll


def encode(api, context, pcm):
    wire = C.create_string_buffer(120)
    n = api.bh_voice_encode_f32(context, pcm.ctypes.data, len(pcm), wire, len(wire))
    assert n > 0
    return wire.raw[:n]


@pytest.mark.parametrize('codec,rate', [(1, 8000), (2, 16000), (3, 32000)])
def test_native_voice_keeps_frame_duration_reset_and_concealment_bounds(api, codec, rate):
    context = api.bh_voice_create_at(codec, rate)
    assert context and api.bh_voice_pcm_rate(context) == rate
    frame = rate * 75 // 10000
    try:
        pcm = (.2*np.sin(np.arange(frame)*2*np.pi*997/rate)).astype('f4')
        for _ in range(40):
            wire = encode(api, context, pcm)
            assert api.bh_voice_push_rx(context, wire, len(wire), 0) >= 0
        output = np.empty(frame*40, 'f4')
        # Exactly 32 frames worth of time at every PCM rate, not 32*360 samples.
        assert api.bh_voice_read_f32(context, output.ctypes.data, len(output)) == frame*32
        assert np.isfinite(output[:frame*32]).all()
        assert api.bh_voice_reset(context) == 0
        assert api.bh_voice_conceal(context, 2) == 2
        assert api.bh_voice_read_f32(context, output.ctypes.data, len(output)) == frame*2
    finally:
        api.bh_voice_destroy(context)


def test_cvsd_hci_integer_samples_are_exact_without_intermediate_src(api):
    context = api.bh_voice_create_at(1, 8000)
    rng = np.random.default_rng(212)
    samples = rng.integers(-32768, 32768, 600, dtype=np.int16)
    pcm = (samples.astype('f4') / 32768).reshape(-1, 60)
    try:
        wires = [encode(api, context, block) for block in pcm]
        assert b''.join(wires) == samples.tobytes()
        packet = b''.join(wires)
        offset = 0
        for n in (1, 3, 59, 120, 257, 760):
            block = packet[offset:offset+n]
            assert api.bh_voice_push_rx(context, block, len(block), 0) >= 0
            offset += len(block)
        output = np.empty(600, 'f4')
        assert api.bh_voice_read_f32(context, output.ctypes.data, len(output)) == 600
        np.testing.assert_array_equal(output, samples.astype('f4') / 32768)
    finally:
        api.bh_voice_destroy(context)


def test_msbc_native_decoder_matches_ffmpeg_s16_pcm_sample_for_sample(api):
    encoder = api.bh_voice_create_at(2, 16000)
    decoder = api.bh_voice_create_at(2, 16000)
    reference = av.CodecContext.create('sbc', 'r')
    reference.sample_rate = 16000
    reference.layout = 'mono'
    try:
        for frame in range(15):
            t = (np.arange(120) + frame*120) / 16000
            pcm = (.2*np.sin(2*np.pi*997*t)+.03*np.cos(2*np.pi*6101*t)).astype('f4')
            wire = encode(api, encoder, pcm)
            assert api.bh_voice_push_rx(decoder, wire, len(wire), 0) == 1
            output = np.empty(120, 'f4')
            assert api.bh_voice_read_f32(decoder, output.ctypes.data, len(output)) == 120
            frames = reference.decode(av.Packet(wire[2:59]))
            expected = np.concatenate([f.to_ndarray().reshape(-1) for f in frames]).astype('f4')/32768
            np.testing.assert_array_equal(output, expected)
    finally:
        api.bh_voice_destroy(encoder)
        api.bh_voice_destroy(decoder)


def test_lc3_native_float_does_not_lose_quiet_signal_at_s16_precision(api):
    quiet, silence = api.bh_voice_create_at(3, 32000), api.bh_voice_create_at(3, 32000)
    try:
        t = np.arange(240) / 32000
        pcm = (2e-6*np.sin(2*np.pi*1000*t)).astype('f4')
        assert encode(api, quiet, pcm) != encode(api, silence, np.zeros(240, 'f4'))
    finally:
        api.bh_voice_destroy(quiet)
        api.bh_voice_destroy(silence)


@pytest.mark.parametrize('codec,rate', [(1, 16000), (2, 32000), (3, 96000), (0, 48000)])
def test_unsupported_native_format_is_rejected_before_consuming_state(api, codec, rate):
    assert not api.bh_voice_create_at(codec, rate)


def test_native_microphone_formats_hot_switch_without_changing_voice_session(api):
    receiver = api.bh_voice_create_at(2, 16000)
    decoded = []
    try:
        with NativeClient(spawn=True) as client:
            client.request('mix_add', key='call:rates')
            epoch = client.request('voice_start', key='rates', handle=2, codec=2)['epoch']
            for rate in (96000, 44100, 48000):
                ticket = client.request('test_audio_switch', direction='input', endpoint_id=str(rate), sample_rate=rate, voice_epoch=epoch)
                client.wait_audio(ticket)
                for _ in range(22):
                    client.request('test_capture', frames=rate//100)
                    time.sleep(.01)
                    for _ in range(2):
                        wire = bytes.fromhex(client.request('test_voice_tx')['packet'])
                        if wire:
                            assert api.bh_voice_push_rx(receiver, wire, len(wire), 0) >= 0
                            pcm = np.empty(360, 'f4')
                            n = api.bh_voice_read_f32(receiver, pcm.ctypes.data, len(pcm))
                            decoded.append(pcm[:n].copy())
                stats = client.request('snapshot')
                assert stats['voice']['active'] and stats['voice']['epoch'] == epoch
                assert stats['voice']['pcm_rate'] == 16000
                assert stats['uplink']['microphone']['pcm_rate'] == rate
                assert stats['voice']['decode_errors'] == stats['bridge_errors'] == 0
        assert np.sqrt(np.mean(np.concatenate(decoded)**2)) > .03
    finally:
        api.bh_voice_destroy(receiver)

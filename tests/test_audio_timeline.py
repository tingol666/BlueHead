"""Presentation deadlines, source clocks and native-worker isolation; no devices."""
import base64
import ctypes as C
import json
from pathlib import Path
import struct
import time

import numpy as np
import pytest

from bluehead.media import NativeMedia
from bluehead.native_runtime import NativeClient
from bluehead.protocol import Format
from .test_continuity import core, render
from .test_native_runtime import wait_done


def timeline_api():
    api = core()
    api.bh_mix_timeline.argtypes = [C.c_void_p, C.c_int, C.c_int]
    api.bh_mix_observe.argtypes = [C.c_void_p, C.c_int, C.c_uint64, C.c_uint64]
    api.bh_mix_clear.argtypes = [C.c_void_p, C.c_int]
    return api


def signal(first, count):
    t = np.arange(first, first+count) / 48000
    return np.column_stack((.15*np.sin(2*np.pi*197*t)+.06*np.cos(2*np.pi*17431*t),
                            .13*np.sin(2*np.pi*1451*t)+.05*np.cos(2*np.pi*11053*t))).astype('f4')


@pytest.mark.parametrize('owner', [1, 2, 3])
def test_framed_unity_preserves_signal_without_callback_search(owner):
    api = timeline_api()
    mixer = api.bh_mix_create()
    source = api.bh_mix_add(mixer, 0, 48000)
    api.bh_mix_continuity(mixer, source, 1, 0, 0)
    api.bh_mix_timeline(mixer, source, owner)
    data = signal(0, 9000)
    try:
        api.bh_mix_write(mixer, source, data.ctypes.data, len(data))
        output = np.concatenate([render(api, mixer, n) for n in (1, 19, 127, 480, 997, 333, 2048)])
        np.testing.assert_array_equal(output[72:], data[:len(output)-72])
        render(api, mixer, 6000)  # force starvation without moving prediction onto the callback
        assert api.bh_mix_value(mixer, source, 17) == 0
        assert api.bh_mix_value(mixer, source, 14) == owner
    finally:
        api.bh_mix_free(mixer)


def test_late_pcm_is_expired_once_instead_of_playing_the_same_gap_twice():
    api = timeline_api()
    mixer = api.bh_mix_create()
    source = api.bh_mix_add(mixer, 0, 8640)
    api.bh_mix_continuity(mixer, source, 1, 0, 0)
    api.bh_mix_timeline(mixer, source, 1)
    try:
        data = signal(0, 2400)
        api.bh_mix_write(mixer, source, data.ctypes.data, len(data))
        render(api, mixer, 2400)
        render(api, mixer, 240)  # 5 ms presentation deadline passed while the worker was late
        data = signal(2400, 1440)
        api.bh_mix_write(mixer, source, data.ctypes.data, len(data))
        recovered = render(api, mixer, 480)
        assert api.bh_mix_value(mixer, source, 13) == 240
        assert api.bh_mix_value(mixer, source, 10) == 0
        np.testing.assert_allclose(recovered[264:], signal(2640, 480)[192:408], atol=2e-7)
        render(api, mixer, 480)
        assert api.bh_mix_value(mixer, source, 13) == 240
        assert api.bh_mix_value(mixer, source, 17) == 0
    finally:
        api.bh_mix_free(mixer)


def test_long_worker_outage_discards_old_backlog_but_explicit_restart_keeps_new_start():
    api = timeline_api()
    mixer = api.bh_mix_create()
    source = api.bh_mix_add(mixer, 960, 8640)
    api.bh_mix_continuity(mixer, source, 1, 960, 2880)
    api.bh_mix_timeline(mixer, source, 1)
    try:
        data = signal(0, 2400)
        api.bh_mix_write(mixer, source, data.ctypes.data, len(data))
        for _ in range(20):
            render(api, mixer)
        assert api.bh_mix_value(mixer, source, 10) == 1
        data = signal(2400, 6000)
        api.bh_mix_write(mixer, source, data.ctypes.data, len(data))
        render(api, mixer)
        assert api.bh_mix_value(mixer, source, 18) >= 4000
        assert api.bh_mix_value(mixer, source, 0) < 2000
        prior = api.bh_mix_value(mixer, source, 18)
        api.bh_mix_clear(mixer, source)
        api.bh_mix_write(mixer, source, data.ctypes.data, 6000)
        render(api, mixer)
        assert api.bh_mix_value(mixer, source, 18) == prior
        assert api.bh_mix_value(mixer, source, 0) > 5000
    finally:
        api.bh_mix_free(mixer)


@pytest.mark.parametrize('codec,frames', [('sbc', 128), ('aac', 1024), ('aptx_hd', 1024)])
def test_original_packet_timing_is_independent_of_decoder_drain_bursts(codec, frames):
    api = timeline_api()
    frequency = C.c_longlong()
    C.windll.kernel32.QueryPerformanceFrequency(C.byref(frequency))
    vectors = json.loads((Path(__file__).parents[1]/'bluehead/assets/selfcheck_vectors.json').read_text('utf-8'))
    packets = next(v['packets'] for v in vectors['vectors'] if v['codec'] == codec)
    mixer = api.bh_mix_create()
    source = api.bh_mix_add(mixer, 2880, 8640)
    api.bh_mix_continuity(mixer, source, 1, 1440, 5760)
    api.bh_mix_timeline(mixer, source, 1)
    decoder = NativeMedia(Format(codec, 48000, 2))
    decoder.api.bh_media_continuity.argtypes = [C.c_void_p, C.c_int]
    decoder.api.bh_media_continuity(decoder.handle, 1)
    drain = decoder.api.bh_media_drain_mixer_at
    drain.argtypes = [C.c_void_p, C.c_void_p, C.c_int, C.c_uint64]
    try:
        for i in range(40):
            data = bytearray(base64.b64decode(packets[i % len(packets)]))
            struct.pack_into('>HI', data, 2, i, (0xffffe000+i*frames) & 0xffffffff)
            decoder.decode_packet(bytes(data))
            # Drains happen back-to-back but packet receipt retains its own clock.
            arrival = 1_000_000 + round(i*frames/48000*frequency.value)
            drain(decoder.handle, mixer, source, arrival)
        assert api.bh_mix_value(mixer, source, 6) <= 1
        assert decoder.value(12) == decoder.frames
        assert decoder.value(13) == 0
        assert decoder.value(11) == 0
    finally:
        decoder.close()
        api.bh_mix_free(mixer)


@pytest.mark.parametrize('direction', ['media', 'voice'])
def test_native_worker_stall_does_not_stop_other_directions_and_recovers(direction):
    with NativeClient(spawn=True) as client:
        client.request('test_replay', seconds=4, codec='aac', voice_codec=3)
        time.sleep(1)
        before = client.request('snapshot')
        client.request('test_stall', direction=direction, milliseconds=150)
        time.sleep(.30)
        after = client.request('snapshot')
        assert after['injected_native_stalls'] == 1
        assert after['microphone_frames'] > before['microphone_frames'] + 8000
        if direction == 'media':
            assert after['voice']['rx_packets'] > before['voice']['rx_packets'] + 20
            assert after['voice']['tx_packets'] > before['voice']['tx_packets'] + 20
        else:
            assert after['sources']['offline:media']['media']['decoded_frames'] > before['sources']['offline:media']['media']['decoded_frames'] + 8000
        report = wait_done(client)
        assert report['codec_errors'] == report['compressed_queue_drops'] == 0
        snapshot = client.request('snapshot')
        for value in snapshot['sources'].values():
            assert value['callback_period_searches'] == 0
        assert snapshot['sources']['offline:media']['jitter_ms'] < 10
        assert snapshot['sources']['offline:media']['queued_frames'] < 8640


def test_production_rejects_native_stall_injection():
    with NativeClient(spawn=True, offline=False) as client:
        with pytest.raises(OSError, match='[Oo]ffline-test'):
            client.request('test_stall', direction='media', milliseconds=1)


@pytest.mark.parametrize('codec,frames', [('sbc', 128), ('aac', 1024), ('aptx_hd', 1024)])
def test_forward_and_backward_seeks_reset_timeline_without_fabricating_a_long_gap(codec, frames):
    vectors = json.loads((Path(__file__).parents[1]/'bluehead/assets/selfcheck_vectors.json').read_text('utf-8'))
    packets = next(v['packets'] for v in vectors['vectors'] if v['codec'] == codec)
    decoder = NativeMedia(Format(codec, 48000, 2))
    decoder.api.bh_media_continuity.argtypes = [C.c_void_p, C.c_int]
    decoder.api.bh_media_continuity(decoder.handle, 1)
    try:
        for i in range(90):
            data = bytearray(base64.b64decode(packets[i % len(packets)]))
            position = i*frames + (480000 if 30 <= i < 60 else 0)
            struct.pack_into('>HI', data, 2, i, position)
            decoder.decode_packet(bytes(data))
            decoder.read_pcm()
        assert decoder.value(11) == 2
        assert decoder.value(10) == 0
        assert decoder.frames > frames*80
        assert decoder.value(3) == 0
    finally:
        decoder.close()


def test_capture_worker_stall_keeps_callback_and_control_unblocked_then_retires_stale_pcm():
    with NativeClient(spawn=True) as client:
        ticket = client.request('test_audio_switch', direction='input')
        for _ in range(100):
            if client.request('audio_result', ticket=ticket).get('done'):
                break
            time.sleep(.005)
        client.request('test_stall', direction='capture', milliseconds=300)
        for _ in range(100):
            if client.request('snapshot')['injected_native_stalls']:
                break
            time.sleep(.005)
        began = time.monotonic()
        for _ in range(12):
            client.request('test_capture', frames=480)
        assert time.monotonic()-began < .2
        time.sleep(.35)
        before = client.request('snapshot')['microphone_frames']
        client.request('test_capture', frames=480)
        time.sleep(.05)
        after = client.request('snapshot')
        assert after['microphone_frames'] >= before+480
        assert after['bridge_errors'] == 0
        assert after['uplink']['microphone']['gap_owner'] == 3

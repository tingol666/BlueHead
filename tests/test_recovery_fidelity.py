"""Concealment changes only a bounded event window, at native PCM rates."""
import math
import struct
import time
from types import SimpleNamespace

import numpy as np
import pytest

from bluehead.ldac import _NativeMedia
from bluehead.native_runtime import NativeClient
from bluehead.realtime import NativeRender
from .test_native_rates import create, render
from .vectors import LdacEncoder, rtp, signal


def value(decoder, field):
    return int(decoder.api.bh_ldac_media_value(decoder.handle, field))


def fixture(rate):
    encoder = LdacEncoder(rate, 0)
    try:
        payloads = encoder.encode(signal(rate, .35))
    finally:
        encoder.close()
    positions, position = [], 0
    for payload in payloads:
        positions.append(position)
        position += (payload[0] & 31) * (256 if rate >= 88200 else 128)
    packets = [rtp(p, i, (0xfffff000+positions[i]) & 0xffffffff) for i, p in enumerate(payloads)]
    return packets, positions


def decode(decoder, packets, excluded=()):
    output = []
    for i, packet in enumerate(packets):
        if i in excluded:
            continue
        assert decoder.decode_packet(packet) == 0
        output.append(np.frombuffer(decoder.read_pcm(), 'f4').reshape(-1, 2).copy())
    return np.concatenate(output)


@pytest.mark.parametrize('rate', [44100, 48000, 96000])
@pytest.mark.parametrize('lost', [1, 4])
def test_ldac_loss_keeps_full_timeline_and_recovers_exact_pcm_outside_event(rate, lost):
    packets, positions = fixture(rate)
    reference, impaired = _NativeMedia(rate, rate), _NativeMedia(rate, rate)
    try:
        baseline = decode(reference, packets)
        start, stop = positions[8], positions[8+lost]
        recovered = decode(impaired, packets, range(8, 8+lost))
        assert len(recovered) == len(baseline)
        assert value(impaired, 10) == stop-start
        assert value(impaired, 0) == len(baseline)-(stop-start)
        assert value(impaired, 3) == value(impaired, 9) == value(impaired, 11) == 0
        np.testing.assert_array_equal(recovered[:start], baseline[:start])
        after = stop + math.ceil(rate*.004)
        np.testing.assert_array_equal(recovered[after:], baseline[after:])
        if stop-start > math.ceil(rate*.02):
            assert not recovered[start+math.ceil(rate*.02):stop].any()
    finally:
        reference.close()
        impaired.close()


@pytest.mark.parametrize('rate', [44100, 96000])
def test_ldac_forward_backward_seek_discards_old_queue_without_a_fabricated_long_gap(rate):
    packets, _ = fixture(rate)
    decoder = _NativeMedia(rate, rate)
    try:
        for i, packet in enumerate(packets):
            stamp = int.from_bytes(packet[4:8], 'big')
            if 10 <= i < 20:
                stamp = (stamp + rate*5) & 0xffffffff
            changed = packet[:4] + stamp.to_bytes(4, 'big') + packet[8:]
            assert decoder.decode_packet(changed) == 0
            # Intentionally leave the previous segment queued at both seeks.
            if i in (10, 20):
                expected = (packet[12] & 31) * (256 if rate >= 88200 else 128)
                assert value(decoder, 7) == expected
        assert value(decoder, 11) == 2
        assert value(decoder, 10) == value(decoder, 3) == 0
    finally:
        decoder.close()


def test_explicit_ldac_restart_has_no_old_pcm_or_recovery_tail():
    packets, _ = fixture(96000)
    old, fresh = _NativeMedia(96000, 96000), _NativeMedia(96000, 96000)
    try:
        decode(old, packets[:12])
        old.restart()
        np.testing.assert_array_equal(decode(old, packets[12:]), decode(fresh, packets[12:]))
        assert value(old, 10) == value(old, 11) == 0
    finally:
        old.close()
        fresh.close()


@pytest.mark.parametrize('rate', [44100, 96000, 192000])
def test_callback_short_deadline_gap_expires_once_and_preserves_samples_after_merge(rate):
    rng = np.random.default_rng(212)
    pcm = rng.uniform(-.3, .3, (18000, 2)).astype('f4')
    api, mixer, source = create(rate, pcm[:6000])
    renderer = NativeRender(SimpleNamespace(handle=mixer), rate)
    missing = rate // 400
    delay, merge = (rate*3+1999)//2000, math.ceil(rate*.004)
    try:
        render(renderer, 6000)
        render(renderer, missing)
        api.bh_mix_write(mixer, source, pcm[6000:].ctypes.data, 12000)
        recovered = render(renderer, merge+delay+1000)
        after = merge+delay
        np.testing.assert_array_equal(recovered[after:], pcm[6000+missing+merge:6000+missing+merge+1000])
        assert api.bh_mix_value(mixer, source, 13) == missing*48000//rate
        assert api.bh_mix_value(mixer, source, 10) == 0
        assert api.bh_mix_value(mixer, source, 17) == 0
        assert abs(api.bh_mix_value(mixer, source, 9) - merge*48000/rate) <= 1
    finally:
        renderer.close()
        api.bh_mix_free(mixer)


def test_native_ldac_child_restart_keeps_cumulative_supply_and_error_counters():
    packets, _ = fixture(96000)
    with NativeClient(spawn=True) as client:
        client.request('mix_add', key='ldac-counters')
        client.request('media_config', key='ldac-counters', codec=5, rate=96000, channels=2)
        client.request('media_route', key='ldac-counters', handle=1, cid=64)
        client.request('media_pause', key='ldac-counters', paused=False)

        def send(packet):
            old = client.request('media_snapshot', key='ldac-counters')
            acl = struct.pack('<HHHH', 1, len(packet)+4, len(packet), 64)+packet
            client.request('test_hci_acl', packet=acl.hex())
            end = time.monotonic()+2
            while time.monotonic() < end:
                new = client.request('media_snapshot', key='ldac-counters')
                if new['decoded_frames'] > old['decoded_frames'] or new['decode_errors'] > old['decode_errors']:
                    return new
                time.sleep(.001)
            raise AssertionError('LDAC worker did not process the packet')

        for i in range(10):
            if i != 4:
                send(packets[i])
        # A structurally valid frame with a damaged coded body resets only
        # the child decoder. Those counters must survive the next child epoch.
        payload = packets[10][12:]
        length = (((payload[1] << 16 | payload[2] << 8 | payload[3]) >> 2) & 511)+4
        damaged = b'\x01' + payload[1:4] + b'\xff'*(length-3)
        before = send(rtp(damaged, 10, int.from_bytes(packets[10][4:8], 'big')))
        assert before['lost_packets'] >= 1 and before['decode_errors'] >= 1
        client.request('media_pause', key='ldac-counters', paused=True)
        client.request('media_pause', key='ldac-counters', paused=False)
        after = send(packets[11])
        assert after['decoded_frames'] > before['decoded_frames']
        assert after['coded_bytes'] > before['coded_bytes']
        for key in ('lost_packets', 'decode_errors', 'concealed_frames', 'recoveries'):
            assert after[key] >= before[key]

"""Synthetic silence/weak-signal boundaries, before and after native rendering.

The pinned AOSP encoder deliberately writes a nonzero normalized placeholder
when a quantization unit has scale-factor ID zero. It is not source audio.
No physical audio endpoint, controller, microphone or copyrighted media is used.
"""
from types import SimpleNamespace
import ctypes as C
import struct
import time

import numpy as np
import pytest

from bluehead.ldac import NativeLdac, _NativeMedia
from bluehead.media import NativeMedia
from bluehead.protocol import Format, ldac_frames
from bluehead.realtime import NativeRender
from bluehead.native_runtime import NativeClient
from .test_native_rates import create, render
from .vectors import LdacEncoder, encode, rtp, signal, wire_packets, quality


def raw_ldac(pcm, rate, mode=0):
    encoder, decoder = LdacEncoder(rate, mode), NativeLdac()
    try:
        output = []
        for payload in encoder.encode(pcm):
            for frame in ldac_frames(payload, Format('ldac', rate, 2)):
                output.append(np.frombuffer(decoder.decode_bytes(frame), 'f4').reshape(-1, 2))
        return np.concatenate(output)
    finally:
        encoder.close()
        decoder.close()


@pytest.mark.parametrize('rate', [44100, 48000, 88200, 96000])
@pytest.mark.parametrize('mode', [0, 1, 2], ids=['high', 'standard', 'mobile'])
def test_ldac_encoded_digital_silence_is_zero_from_first_sample(rate, mode):
    output = raw_ldac(np.zeros((rate // 5, 2), 'f4'), rate, mode)
    assert len(output) > rate // 10
    assert not output.any(), (rate, mode, float(np.max(abs(output))))


@pytest.mark.parametrize('rate', [44100, 48000, 88200, 96000])
@pytest.mark.parametrize('mode', [0, 1, 2], ids=['high', 'standard', 'mobile'])
def test_ldac_silence_after_music_does_not_repeat_a_tone(rate, mode):
    pcm = np.zeros((rate * 3 // 5, 2), 'f4')
    pcm[rate // 10:rate // 4] = signal(rate, .15)
    output = raw_ldac(pcm, rate, mode)
    # The finite transform overlap is valid tail audio. Well past that overlap,
    # encoded silence must remain zero, independent of the preceding music.
    assert not output[:rate // 20].any()
    assert not output[rate * 2 // 5:].any()


@pytest.mark.parametrize('rate', [44100, 48000, 96000])
@pytest.mark.parametrize('db', [-60, -100, -130])
def test_ldac_quiet_tone_survives_while_other_channel_stays_silent(rate, db):
    amplitude = 10 ** (db / 20)
    time = np.arange(rate // 4) / rate
    pcm = np.column_stack((amplitude * np.sin(2 * np.pi * 1000 * time),
                           np.zeros_like(time))).astype('f4')
    output = raw_ldac(pcm, rate)
    middle = output[rate // 20:rate * 3 // 20]
    basis = np.exp(-2j * np.pi * 1000 * np.arange(len(middle)) / rate)
    measured = abs(np.dot(middle[:, 0], basis)) * 2 / len(middle)
    assert .97 < measured / amplitude < 1.03, (rate, db, measured / amplitude)
    assert not middle[:, 1].any()


@pytest.mark.parametrize('rate,output_rate', [(44100, 48000), (48000, 96000),
                                            (88200, 48000), (96000, 96000)])
def test_ldac_silence_remains_zero_through_media_src_and_native_render(rate, output_rate):
    encoder, decoder = LdacEncoder(rate, 0), _NativeMedia(rate, rate)
    mixer = renderer = None
    try:
        pieces, timestamp = [], 0
        for sequence, payload in enumerate(encoder.encode(np.zeros((rate // 5, 2), 'f4'))):
            assert decoder.decode_packet(rtp(payload, sequence, timestamp)) == 0
            timestamp += (payload[0] & 31) * (256 if rate >= 88200 else 128)
            pieces.append(np.frombuffer(decoder.read_pcm(), 'f4').reshape(-1, 2))
        pcm = np.concatenate(pieces)
        api, mixer, source = create(rate, pcm)
        renderer = NativeRender(SimpleNamespace(handle=mixer), output_rate)
        assert not render(renderer, output_rate // 2).any()
        assert decoder.values()[3] == 0
        assert api.bh_mix_value(mixer, source, 17) == 0  # No callback period search.
    finally:
        if renderer:
            renderer.close()
        if mixer:
            api.bh_mix_free(mixer)
        encoder.close()
        decoder.close()


@pytest.mark.parametrize('codec', ['sbc', 'aac', 'aptx', 'aptx_hd'])
def test_other_media_codecs_do_not_create_a_sustained_silence_tone(codec):
    decoder = NativeMedia(Format(codec, 48000, 2), 48000)
    try:
        output = []
        for packet in wire_packets(codec, encode(codec, np.zeros((24000, 2), 'f4'))):
            decoder.decode_packet(packet)
            output.append(np.frombuffer(decoder.read_pcm(), 'f4').reshape(-1, 2))
        pcm = np.concatenate(output)
        # Integer SBC/aptX have codec quantization residue; use a bounded codec
        # floor rather than incorrectly treating all nonzero PCM as a defect.
        tail = pcm[4800:]
        assert decoder.errors == 0
        assert len(tail) > 12000
        assert np.max(abs(tail)) <= 2 / 32768, (codec, float(np.max(abs(tail))))
    finally:
        decoder.close()


def ldac_packets(pcm, rate):
    encoder = LdacEncoder(rate, 0)
    try:
        timestamp = 0
        packets = []
        for sequence, payload in enumerate(encoder.encode(pcm)):
            packets.append(rtp(payload, sequence, timestamp))
            timestamp += (payload[0] & 31) * (256 if rate >= 88200 else 128)
        return packets
    finally:
        encoder.close()


@pytest.mark.parametrize('rate,output_rate', [(44100, 48000), (48000, 48000),
                                            (88200, 96000), (96000, 48000), (96000, 96000)])
def test_ldac_restart_removes_old_music_from_decoder_src_and_unread_queue(rate, output_rate):
    old, fresh = _NativeMedia(rate, output_rate), _NativeMedia(rate, output_rate)

    def decode(decoder, packets):
        output = []
        for packet in packets:
            assert decoder.decode_packet(packet) == 0
            output.append(decoder.read_pcm())
        return b''.join(output)

    try:
        for packet in ldac_packets(signal(rate, .15), rate):
            assert old.decode_packet(packet) == 0
        old.restart()
        assert old.values()[7] == 0
        silence = ldac_packets(np.zeros((rate // 4, 2), 'f4'), rate)
        actual = decode(old, silence)
        assert actual == decode(fresh, silence)
        assert len(actual) > output_rate
        assert not np.frombuffer(actual, 'f4').any()
    finally:
        old.close()
        fresh.close()


@pytest.mark.parametrize('rate,output_rate', [(44100, 48000), (96000, 96000)])
@pytest.mark.parametrize('event', ['lost', 'damaged'])
def test_ldac_loss_or_damaged_frame_near_pause_cannot_leave_a_sustained_tail(rate, output_rate, event):
    pcm = np.zeros((rate * 3 // 5, 2), 'f4')
    pcm[:rate // 10] = signal(rate, .1)
    packets = ldac_packets(pcm, rate)
    decoder = _NativeMedia(rate, output_rate)
    output = []
    try:
        for index, packet in enumerate(packets):
            stamp = int.from_bytes(packet[4:8], 'big')
            if rate // 10 <= stamp < rate * 3 // 25:
                if event == 'lost':
                    continue
                # Preserve a valid frame header; force a rejected coded body.
                payload = packet[12:]
                length = (((payload[1] << 16 | payload[2] << 8 | payload[3]) >> 2) & 511) + 4
                damaged = b'\x01' + payload[1:4] + b'\xff' * (length - 3)
                assert decoder.decode_packet(rtp(damaged, index, stamp)) == -2
                continue
            assert decoder.decode_packet(packet) == 0
            output.append(np.frombuffer(decoder.read_pcm(), 'f4').reshape(-1, 2).copy())
        actual = np.concatenate(output)
        assert len(actual) > output_rate // 2
        assert not actual[output_rate * 3 // 10:].any()
        assert decoder.api.bh_ldac_media_value(decoder.handle, 10) > 0
        assert decoder.values()[3] > 0 if event == 'damaged' else decoder.values()[5] > 0
    finally:
        decoder.close()


@pytest.mark.parametrize('source_rate,output_rate', [(44100, 44100), (48000, 96000),
                                                   (96000, 48000), (192000, 44100)])
@pytest.mark.parametrize('owner', [1, 2, 3], ids=['media', 'voice', 'capture'])
def test_renderer_tail_and_explicit_clear_cannot_sustain_previous_audio(source_rate, output_rate, owner):
    pcm = signal(source_rate, .1)
    api, mixer, source = create(source_rate, pcm)
    api.bh_mix_clear.argtypes = [C.c_void_p, C.c_int]
    assert api.bh_mix_timeline(mixer, source, owner) == 0
    renderer = NativeRender(SimpleNamespace(handle=mixer), output_rate)
    try:
        output = render(renderer, output_rate * 3 // 10)
        # Finite filter support, the 1.5ms lookahead and deadline envelopes may
        # finish the tail. They must not manufacture a sustained tone on stop.
        assert np.max(abs(output[:output_rate // 10])) > .1
        assert not output[output_rate * 3 // 20:].any()
        api.bh_mix_write(mixer, source, pcm.ctypes.data, len(pcm))
        render(renderer, output_rate // 25)
        api.bh_mix_clear(mixer, source)
        silence = np.zeros((source_rate // 5, 2), 'f4')
        api.bh_mix_write(mixer, source, silence.ctypes.data, len(silence))
        assert not render(renderer, output_rate // 10).any()
        assert api.bh_mix_value(mixer, source, 17) == 0
    finally:
        renderer.close()
        api.bh_mix_free(mixer)


@pytest.mark.parametrize('codec,rate', [('sbc', 48000), ('aac', 48000), ('aptx', 48000),
                                      ('aptx_hd', 48000), ('ldac', 96000)])
def test_actual_native_hci_worker_pause_restart_and_no_packets_do_not_create_a_tone(codec, rate):
    def packets(pcm):
        if codec == 'ldac':
            return ldac_packets(pcm, rate)
        return list(wire_packets(codec, encode(codec, pcm, rate), rate))

    music = packets(signal(rate, .12))
    silence = packets(np.zeros((rate // 5, 2), 'f4'))
    with NativeClient(spawn=True) as client:
        key = 'synthetic-boundary'
        client.request('mix_add', key=key)
        code = {'sbc': 1, 'aac': 2, 'aptx': 3, 'aptx_hd': 4, 'ldac': 5}[codec]
        client.request('media_config', key=key, codec=code, rate=rate, channels=2)
        client.request('media_route', key=key, handle=1, cid=64)
        client.request('media_pause', key=key, paused=False)

        def feed(data):
            for packet in data:
                before = client.request('media_snapshot', key=key)['coded_bytes']
                acl = struct.pack('<HHHH', 1, len(packet) + 4, len(packet), 64) + packet
                client.request('test_hci_acl', packet=acl.hex())
                end = time.monotonic() + 2
                while time.monotonic() < end:
                    state = client.request('media_snapshot', key=key)
                    # coded_bytes is committed after decode/drain; SBC can
                    # retain its first frame in the parser until the next one.
                    if state['coded_bytes'] > before:
                        break
                    time.sleep(.001)
                else:
                    raise AssertionError('Native media worker failed to supply PCM')
            # Last PCM publication must also have committed before pause/inspection.
            time.sleep(.03)

        feed(music)
        assert client.request('test_render', frames=2400)['peak'] > .01
        client.request('media_pause', key=key, paused=True)
        assert client.request('test_render', frames=4800)['peak'] == 0


        client.request('media_pause', key=key, paused=False)
        feed(silence)
        state = client.request('media_snapshot', key=key)
        assert state['decode_errors'] == state['dropped_packets'] == 0
        assert client.request('test_render', frames=4800)['peak'] <= 2 / 32768
        # Stop sending any packet; drain all queues, then test the stale-meter
        # window too. Statistics timing has no authority to synthesize audio.
        for _ in range(4):
            client.request('test_render', frames=4800)
        assert client.request('test_render', frames=4800)['peak'] == 0


@pytest.mark.parametrize('rate,minimum_snr', [(48000, 110), (96000, 104)])
def test_ldac_high_quality_multitone_preserves_fine_quantization_detail(rate, minimum_snr):
    pcm = signal(rate, .35)
    output = raw_ldac(pcm, rate, 0)
    # This deterministic wideband fixture exercises coded residual precision.
    # The faulty coarse-index dequantizer only reaches about 94dB here.
    snr, _ = quality(pcm, output)
    assert snr > minimum_snr, (rate, snr)

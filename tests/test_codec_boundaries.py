"""Media/voice silence and reset boundaries against independent PCM references.

No hardware, noise gate, level normalization or listening threshold is involved.
Lossy codec residue is compared with its decoder, not mistaken for source music.
"""
import av
import numpy as np
import pytest

from bluehead.media import NativeMedia
from bluehead.protocol import Format
from .test_voice_native_rates import api as api, encode as voice_encode  # noqa: F401
from .test_native_voice import dll as dll  # noqa: F401
from .vectors import encode, signal, wire_packets as wire

CODECS = ['sbc', 'aac', 'aptx', 'aptx_hd']


def decode(decoder, packets):
    output = []
    for packet in packets:
        decoder.decode_packet(packet)
        output.append(np.frombuffer(decoder.read_pcm(), 'f4').reshape(-1, 2).copy())
    return np.concatenate(output)


def reference(codec, packets, rate):
    context = av.CodecContext.create(codec, 'r')
    context.sample_rate, context.layout = rate, 'stereo'
    output = []
    for packet in packets:
        for frame in context.decode(av.Packet(packet)):
            data = frame.to_ndarray()
            data = data.T if frame.format.is_planar else data.reshape(-1, 2)
            if data.dtype.kind == 'i':
                data = data.astype('f8') / 2 ** (data.dtype.itemsize * 8 - 1)
            output.append(data.astype('f4'))
    return np.concatenate(output)


@pytest.mark.parametrize('codec', CODECS)
@pytest.mark.parametrize('rate', [44100, 48000])
def test_silent_music_silent_boundary_matches_independent_decoder_without_extra_signal(codec, rate):
    pcm = np.zeros((rate * 3 // 5, 2), 'f4')
    pcm[rate // 10:rate * 3 // 10] = signal(rate, .2)
    packets = encode(codec, pcm, rate)
    expected = reference(codec, packets, rate)
    decoder = NativeMedia(Format(codec, rate, 2), rate)
    try:
        actual = decode(decoder, wire(codec, packets, rate))
        # SBC's streaming parser may hold its last frame, as in production.
        assert 0 <= len(expected) - len(actual) <= (128 if codec == 'sbc' else 0)
        np.testing.assert_allclose(actual, expected[:len(actual)], rtol=0, atol=2e-7 if codec == 'aac' else 0)
        assert decoder.errors == decoder.value(13) == 0  # Direct native-rate PCM.
        assert np.max(abs(actual[rate // 2:])) <= 2 / 32768
    finally:
        decoder.close()


@pytest.mark.parametrize('codec', CODECS)
@pytest.mark.parametrize('rate,output_rate', [(44100, 44100), (48000, 96000)])
def test_explicit_restart_clears_previous_music_parser_overlap_src_and_queue(codec, rate, output_rate):
    music = list(wire(codec, encode(codec, signal(rate, .15), rate), rate))
    silence = list(wire(codec, encode(codec, np.zeros((rate // 4, 2), 'f4'), rate), rate))
    old = NativeMedia(Format(codec, rate, 2), output_rate)
    fresh = NativeMedia(Format(codec, rate, 2), output_rate)
    try:
        # Leave real audio unread as well as in the codec/SRC histories.
        for packet in music:
            old.decode_packet(packet)
        old.restart()
        assert old.value(7) == 0
        actual, expected = decode(old, silence), decode(fresh, silence)
        np.testing.assert_array_equal(actual, expected)
        assert np.max(abs(actual)) <= 2 / 32768
    finally:
        old.close()
        fresh.close()


@pytest.mark.parametrize('codec', CODECS)
@pytest.mark.parametrize('db', [-60, -100])
def test_weak_signal_and_empty_other_channel_match_codec_reference(codec, db):
    rate = 48000
    time = np.arange(rate // 5) / rate
    pcm = np.column_stack((10 ** (db / 20) * np.sin(2 * np.pi * 997 * time),
                           np.zeros_like(time))).astype('f4')
    packets = encode(codec, pcm)
    expected = reference(codec, packets, rate)
    decoder = NativeMedia(Format(codec, rate, 2), rate)
    try:
        actual = decode(decoder, wire(codec, packets, rate))
        np.testing.assert_allclose(actual, expected[:len(actual)], rtol=0, atol=2e-9 if codec == 'aac' else 0)
    finally:
        decoder.close()


@pytest.mark.parametrize('codec', [1, 2, 3], ids=['cvsd-pcm', 'msbc', 'lc3-swb'])
def test_voice_silence_stays_bounded_and_reset_removes_old_music_from_first_frame(api, codec):
    rate = {1: 8000, 2: 16000, 3: 32000}[codec]
    count = rate * 75 // 10000
    encoder, old, fresh = [api.bh_voice_create_at(codec, rate) for _ in range(3)]
    assert encoder and old and fresh

    def receive(context, packet):
        assert api.bh_voice_push_rx(context, packet, len(packet), 0) == 1
        pcm = np.empty(count, 'f4')
        assert api.bh_voice_read_f32(context, pcm.ctypes.data, len(pcm)) == count
        return pcm

    try:
        for index in range(30):
            time = (np.arange(count) + index * count) / rate
            packet = voice_encode(api, encoder, (.2 * np.sin(2 * np.pi * 997 * time)).astype('f4'))
            receive(old, packet)
        # Reset encoder too: a continuing transform bitstream may legitimately
        # carry old music in its first silence frames; that is not a host leak.
        assert api.bh_voice_reset(encoder) == api.bh_voice_reset(old) == 0
        for _ in range(35):
            packet = voice_encode(api, encoder, np.zeros(count, 'f4'))
            actual = receive(old, packet)
            expected = receive(fresh, packet)
            np.testing.assert_array_equal(actual, expected)
            assert np.max(abs(actual)) <= 2 / 32768
    finally:
        for context in (encoder, old, fresh):
            api.bh_voice_destroy(context)

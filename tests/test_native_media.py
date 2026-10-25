import ctypes as c

import numpy as np
import pytest

from bluehead.audio import Mixer
from bluehead.media import NativeMedia
from bluehead.protocol import Format, PacketError
from .vectors import signal, encode, wire_packets, quality, rtp


@pytest.mark.parametrize("codec", ["sbc", "aac", "aptx", "aptx_hd"])
@pytest.mark.parametrize("rate", [44100, 48000])
def test_native_real_codec_round_trip(codec, rate):
    reference = signal(rate)
    decoder = NativeMedia(Format(codec, rate, 2), output_rate=rate)
    try:
        output = []
        for packet in wire_packets(codec, encode(codec, reference, rate), rate):
            output.extend(decoder.feed(packet))
        decoded = np.concatenate(output)
        snr, _ = quality(reference, decoded)
        assert snr > {"sbc": 20, "aac": 18, "aptx": 30, "aptx_hd": 36}[codec]
        assert len(decoded) > len(reference) * .9
        assert decoder.errors == 0 and decoder.value(8) == 0
    finally:
        decoder.close()


def test_native_pcm_never_passes_through_python_array_when_writing_mixer():
    decoder = NativeMedia(Format("sbc", 48000, 2))
    mixer = Mixer(buffer_ms=0)
    mixer.add("source")
    packets = wire_packets("sbc", encode("sbc", signal(48000, .1), 48000), 48000)
    count = 0
    try:
        for packet in packets:
            decoder.decode_packet(packet)
            count += decoder.write_to(mixer, "source")
        assert count > 4000 and mixer.inputs["source"].frames > 0
        assert decoder.value(7) == 0
    finally:
        decoder.close()


def test_native_malformed_rtp_and_bounded_packet_inputs():
    decoder = NativeMedia(Format("aac", 48000, 2))
    for packet in (b"", b"\x80", bytes(12), b"\xb0" + bytes(11), rtp(bytes(8192), 0)):
        with pytest.raises((ValueError, PacketError)):
            decoder.decode_packet(packet)
    assert decoder.value(7) == 0
    decoder.close()


def test_native_sequence_duplicates_and_wrap_are_not_decoded_twice():
    decoder = NativeMedia(Format("sbc", 48000, 2))
    packet = next(iter(wire_packets("sbc", encode("sbc", signal(48000, .1), 48000), 48000)))
    payload = packet[12:]
    try:
        for sequence in (65535, 0, 0, 3, 2):
            decoder.decode_packet(rtp(payload, sequence))
        assert decoder.lost == 2
        assert decoder.frames > 0
    finally:
        decoder.close()


def test_native_rejects_invalid_dimensions_and_safe_double_close():
    decoder = NativeMedia(Format("sbc", 48000, 2))
    for args in ((0, 48000, 2, 48000), (1, 192000, 2, 48000), (1, 48000, 3, 48000)):
        assert not decoder.api.bh_media_create(*args)
    assert decoder.api.bh_media_read(None, None, c.c_uint(-1).value) == 0
    decoder.close()
    decoder.close()

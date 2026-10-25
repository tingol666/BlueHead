import numpy as np
import pytest

from bluehead.audio import Decoder, Mixer
from bluehead.calls import CallRouter
from .voice_reference import Msbc
from bluehead.protocol import Format, Depacketizer, PacketError, parse_rtp, negotiated, capabilities
from .vectors import signal, encode, wire_packets, quality, rtp, LdacEncoder


@pytest.mark.parametrize("codec", ["sbc", "aac", "aptx", "aptx_hd"])
@pytest.mark.parametrize("rate", [44100, 48000])
def test_encoded_audio_round_trip(codec, rate):
    reference = signal(rate)
    decoder = Decoder(Format(codec, rate, 2), output_rate=rate)
    output = []
    for packet in wire_packets(codec, encode(codec, reference, rate), rate):
        output.extend(decoder.feed(packet))
    assert output
    decoded = np.concatenate(output)
    snr, delay = quality(reference, decoded)
    print(f"QUALITY {codec} {rate} Hz: SNR={snr:.2f} dB delay={delay} samples")
    assert snr > {"sbc": 20, "aac": 18, "aptx": 30, "aptx_hd": 36}[codec]
    assert len(decoded) > len(reference) * 0.9
    decoder.close()


@pytest.mark.parametrize("rate", [44100, 48000, 88200, 96000])
@pytest.mark.parametrize("mode", [0, 1, 2])
def test_ldac_reference_encoder(rate, mode):
    from bluehead.ldac import NativeLdac
    encoder = LdacEncoder(rate, mode)
    decoder = NativeLdac()
    fmt = Format("ldac", rate, 2)
    depacketizer = Depacketizer(fmt)
    reference = signal(rate)
    output = []
    try:
        for sequence, payload in enumerate(encoder.encode(reference)):
            for frame in depacketizer.feed(rtp(payload, sequence)):
                output.append(decoder.decode(frame))
        assert output
        decoded = np.concatenate(output)
        snr, delay = quality(reference, decoded)
        print(f"QUALITY LDAC {rate} Hz mode {mode}: SNR={snr:.2f} dB delay={delay} samples")
        # Catch polarity, channel, scaling, frequency and decoder math mistakes.
        assert snr > 24
    finally:
        decoder.close()
        encoder.close()


def test_msbc_voice_roundtrip_and_split_packets():
    speech = signal(16000, 0.3, channels=1).reshape(-1)
    codec = Msbc()
    output = []
    for offset in range(0, len(speech) - 120, 120):
        wire = codec.encode(np.round(speech[offset:offset + 120] * 32767).astype("<i2").tobytes())
        assert len(wire) == 60 and wire[2] == 0xAD
        output.extend(codec.decode(wire[:17]))
        output.extend(codec.decode(wire[17:]))
    decoded = np.concatenate(output).reshape(-1, 1)
    snr, delay = quality(speech.reshape(-1, 1), decoded)
    print(f"QUALITY mSBC voice: SNR={snr:.2f} dB delay={delay} samples")
    assert snr > 15


def test_mixer_simultaneous_sources_and_limiter():
    mixer = Mixer(buffer_ms=0)
    mixer.master = 1
    mixer.add("tablet")
    mixer.add("phone")
    mixer.write("tablet", np.full((480, 2), 0.1, np.float32))
    mixer.write("phone", np.full((480, 2), 0.2, np.float32))
    np.testing.assert_allclose(mixer.render(480), 0.3, atol=1e-6)
    mixer.write("tablet", np.ones((480, 2), np.float32))
    mixer.write("phone", np.ones((480, 2), np.float32))
    assert np.max(np.abs(mixer.render(480))) <= 0.98001


def test_mixer_bounded_latency_and_call_ducking():
    mixer = Mixer(buffer_ms=0, max_ms=100)
    mixer.master = 1
    for key in ("media", "call:phone"):
        mixer.add(key)
        for _ in range(100):
            mixer.write(key, np.full((480, 2), 0.2, np.float32))
        assert mixer.inputs[key].frames <= 4800
    mixer.call_active, mixer.call_source = True, "call:phone"
    np.testing.assert_allclose(mixer.render(480), 0.23, atol=1e-6)


def test_microphone_never_cross_routes():
    router = CallRouter()
    router.select("phone-a")
    router.write_microphone("phone-a", b"abcd")
    assert router.read("phone-b", 4) == bytes(4)
    assert router.read("phone-a", 4) == b"abcd"
    router.write_microphone("phone-a", b"private")
    router.select("phone-b")
    assert router.read("phone-b", 7) == bytes(7)
    router.write_microphone("phone-b", b"speech")
    router.muted = True
    assert router.read("phone-b", 6) == bytes(6)
    router.muted = False
    assert router.read("phone-b", 6) == bytes(6)


def test_rtp_sequence_wrap_loss_and_duplicates():
    dep = Depacketizer(Format("sbc", 48000, 2))
    payload = b"\x01\x9c\x00\x00\x00"
    assert dep.feed(rtp(payload, 65535))
    assert dep.feed(rtp(payload, 0))
    assert not dep.feed(rtp(payload, 0))
    assert dep.feed(rtp(payload, 3))
    assert dep.lost == 2
    assert not dep.feed(rtp(payload, 2))


def test_malformed_rtp_and_codec_configuration():
    for packet in [b"", b"\x80", b"\x80" + bytes(10), bytes(12), b"\xb0" + bytes(11)]:
        with pytest.raises(PacketError):
            parse_rtp(packet)
    for codec in ("aac", "sbc", "aptx", "aptx_hd", "ldac"):
        selected = capabilities(codec, selected=True)
        assert negotiated(codec, [selected]).rate == 48000
        with pytest.raises(PacketError):
            negotiated(codec, [])
        with pytest.raises(PacketError):
            negotiated(codec, [capabilities(codec)])

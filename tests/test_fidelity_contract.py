"""Decoder-output references; no physical endpoints or Bluetooth controllers."""
from pathlib import Path
import os

import numpy as np
import av

from bluehead.ldac import NativeLdac, _NativeMedia
from bluehead.protocol import Format, ldac_frames
from bluehead.media import NativeMedia
from tools.audio_fidelity_audit import mixer_api, unity_measure
from .vectors import LdacEncoder, rtp, signal

ROOT = Path(__file__).resolve().parents[1]


def test_unity_pcm_preserves_24bit_steps_and_low_level_detail_exactly_after_delay():
    with os.add_dll_directory(str(ROOT / 'bluehead/assets')):
        result = unity_measure(mixer_api(ROOT / 'bluehead/assets/bluehead_audio.dll'))
    assert result['delay_frames'] == 72
    assert result['maximum_absolute_error'] == result['rms_error'] == 0
    assert result['exactly_equal_samples'] == result['samples']


def test_ldac_media_at_96k_is_exactly_raw_decoder_pcm_without_intermediate_src():
    rate = 96000
    encoder, raw, media = LdacEncoder(rate, 0), NativeLdac(), _NativeMedia(rate, rate)
    expected, actual = [], []
    try:
        for i, payload in enumerate(encoder.encode(signal(rate, .12))):
            packet = rtp(payload, i)
            expected += [raw.decode_bytes(frame) for frame in ldac_frames(payload, Format('ldac', rate, 2))]
            assert media.decode_packet(packet) >= 0
            actual.append(media.read_pcm())
        reference = np.frombuffer(b''.join(expected), 'f4')
        decoded = np.frombuffer(b''.join(actual), 'f4')
        assert len(reference) > rate // 8
        np.testing.assert_array_equal(decoded, reference)
    finally:
        encoder.close()
        raw.close()
        media.close()


def test_native_mono_mapping_duplicates_decoder_samples_at_unity_without_rematrix_gain():
    encoder = av.CodecContext.create('sbc', 'w')
    encoder.sample_rate, encoder.layout, encoder.format, encoder.bit_rate = 48000, 'mono', 's16', 128000
    encoder.open()
    t = np.arange(4800) / 48000
    pcm = np.rint(12000*np.sin(2*np.pi*997*t)).astype('i2')
    packets = []
    for i in range(0, len(pcm)-encoder.frame_size+1, encoder.frame_size):
        frame = av.AudioFrame.from_ndarray(pcm[i:i+encoder.frame_size].reshape(1, -1), 's16', 'mono')
        frame.sample_rate = 48000
        packets.extend(encoder.encode(frame))
    packets.extend(encoder.encode(None))
    reference = av.CodecContext.create('sbc', 'r')
    reference.sample_rate, reference.layout = 48000, 'mono'
    expected, actual = [], []
    decoder = NativeMedia(Format('sbc', 48000, 1), 48000)
    try:
        for i, packet in enumerate(packets):
            expected.extend(f.to_ndarray().reshape(-1) for f in reference.decode(av.Packet(bytes(packet))))
            decoder.decode_packet(rtp(b'\x01'+bytes(packet), i, i*encoder.frame_size))
            actual.append(np.frombuffer(decoder.read_pcm(), 'f4').reshape(-1, 2))
        output = np.concatenate(actual)
        samples = np.concatenate(expected).astype('f4')/32768
        # The streaming SBC parser can retain its final frame until the next
        # packet. Every PCM sample it actually publishes must match exactly.
        assert 0 <= len(samples)-len(output) <= encoder.frame_size
        np.testing.assert_array_equal(output[:, 0], samples[:len(output)])
        np.testing.assert_array_equal(output[:, 1], samples[:len(output)])
        assert decoder.value(13) == 0  # No software SRC/rematrix path was needed.
    finally:
        decoder.close()

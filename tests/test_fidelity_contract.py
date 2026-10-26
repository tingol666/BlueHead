"""Decoder-output references; no physical endpoints or Bluetooth controllers."""
from pathlib import Path
import os

import numpy as np

from bluehead.ldac import NativeLdac, _NativeMedia
from bluehead.protocol import Format, ldac_frames
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

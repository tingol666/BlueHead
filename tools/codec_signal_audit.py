"""Offline, generated-signal codec audit; no audio, USB or microphone access.

Only numeric results and build digests are saved. The optional old LDAC DLL is
an explicit development input, never part of a public source or runtime package.
"""
import argparse
import ctypes as C
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bluehead.media import NativeMedia  # noqa: E402
from bluehead.protocol import Format, ldac_frames  # noqa: E402
from tests.test_codec_boundaries import decode, reference  # noqa: E402
from tests.vectors import LdacEncoder, encode, quality, signal, wire_packets  # noqa: E402


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def levels(pcm):
    data = pcm.astype('f8')
    rms = float(np.sqrt(np.mean(data * data)))
    return {'rms': rms, 'rms_dbfs': float(20 * np.log10(rms)) if rms else None,
            'peak': float(np.max(abs(data))), 'nonzero_samples': int(np.count_nonzero(data))}


def ldac_decode(library, payloads, rate):
    api = C.CDLL(str(library))
    api.bh_ldac_create.restype = C.c_void_p
    api.bh_ldac_free.argtypes = [C.c_void_p]
    api.bh_ldac_decode.argtypes = [C.c_void_p, C.c_void_p, C.c_int, C.c_void_p, C.c_int]
    context = api.bh_ldac_create()
    if not context:
        raise MemoryError('LDAC test allocation')
    try:
        output = []
        for payload in payloads:
            for frame in ldac_frames(payload, Format('ldac', rate, 2)):
                pcm = np.empty((256, 2), 'f4')
                count = api.bh_ldac_decode(context, frame, len(frame), pcm.ctypes.data, pcm.size)
                if count not in (128, 256):
                    raise ValueError('LDAC test frame rejected')
                output.append(pcm[:count].copy())
        return np.concatenate(output)
    finally:
        api.bh_ldac_free(context)


def ldac_payloads(pcm, rate, mode):
    encoder = LdacEncoder(rate, mode)
    try:
        return encoder.encode(pcm)
    finally:
        encoder.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--baseline-ldac', type=Path)
    args = parser.parse_args()
    current = ROOT / 'bluehead/assets/bluehead_ldac.dll'
    records = []
    for rate in (44100, 48000, 88200, 96000):
        for mode in (0, 1, 2):
            silence = ldac_payloads(np.zeros((rate // 4, 2), 'f4'), rate, mode)
            pcm = signal(rate, .35)
            music = ldac_payloads(pcm, rate, mode)
            item = {'codec': 'ldac', 'rate': rate, 'quality_mode': mode,
                    'silence': levels(ldac_decode(current, silence, rate)),
                    'music_snr_db': quality(pcm, ldac_decode(current, music, rate))[0]}
            if args.baseline_ldac:
                item['baseline_silence'] = levels(ldac_decode(args.baseline_ldac, silence, rate))
                item['baseline_music_snr_db'] = quality(pcm, ldac_decode(args.baseline_ldac, music, rate))[0]
            if item['silence']['nonzero_samples']:
                raise ValueError('LDAC synthesized nonzero encoded silence')
            records.append(item)
    for rate in (44100, 48000):
        for codec in ('sbc', 'aac', 'aptx', 'aptx_hd'):
            pcm = np.zeros((rate * 3 // 5, 2), 'f4')
            pcm[rate // 10:rate * 3 // 10] = signal(rate, .2)
            packets = encode(codec, pcm, rate)
            expected = reference(codec, packets, rate)
            decoder = NativeMedia(Format(codec, rate, 2), rate)
            try:
                actual = decode(decoder, wire_packets(codec, packets, rate))
                error = actual.astype('f8') - expected[:len(actual)]
                peak = float(np.max(abs(error)))
                if peak > (2e-7 if codec == 'aac' else 0):
                    raise ValueError('Media differs from independent codec PCM')
                records.append({'codec': codec, 'rate': rate,
                                'maximum_decoder_reference_error': peak,
                                'leading_silence': levels(actual[:rate // 20]),
                                'trailing_silence': levels(actual[rate // 2:]),
                                'errors': decoder.errors, 'converted_frames': decoder.value(13)})
            finally:
                decoder.close()
    result = {'scope': 'Synthetic, offline; no physical Bluetooth or listening test',
              'ldac_sha256': digest(current), 'audio_sha256': digest(ROOT / 'bluehead/assets/bluehead_audio.dll'),
              'baseline_ldac_sha256': digest(args.baseline_ldac) if args.baseline_ldac else None,
              'records': records}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + '\n', 'utf-8')
    ldac = [r for r in records if r['codec'] == 'ldac']
    other = [r for r in records if r['codec'] != 'ldac']
    print(json.dumps({'ldac_silence_cases': len(ldac), 'ldac_nonzero_samples': sum(
        r['silence']['nonzero_samples'] for r in ldac), 'other_codec_cases': len(other),
        'maximum_other_reference_error': max(r['maximum_decoder_reference_error'] for r in other)}))


if __name__ == '__main__':
    main()

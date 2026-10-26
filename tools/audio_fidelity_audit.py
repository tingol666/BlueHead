"""Measure software signal changes against decoder PCM, without physical I/O.

NumPy and the LDAC test encoder are development dependencies only. Reports do
not contain samples, device addresses, names, recordings or user configuration.
"""
import argparse
import ctypes as C
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.native_build import compiler, includes  # noqa: E402


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_probe():
    output = ROOT / 'work/fidelity-probe'
    output.mkdir(parents=True, exist_ok=True)
    sources = [ROOT / 'tests/native_fidelity_probe.c', ROOT / 'native/audio/audio_mixer.c',
               *(ROOT / 'native/audio').glob('*.h'), ROOT / 'native/audio/audio_mix_continuity.inc']
    source_hash = hashlib.sha256(''.join(digest(p) for p in sources).encode()).hexdigest()
    record = output / 'source.sha256'
    library = output / 'fidelity-probe.dll'
    if not library.exists() or not record.exists() or record.read_text() != source_hash:
        subprocess.run([compiler(), '-std=c11', '-O3', '-shared', '-static-libgcc', '-Wall', '-Wextra',
                        '-Werror', '-Wl,--no-insert-timestamp', *includes(),
                        str(ROOT / 'tests/native_fidelity_probe.c'), '-o', str(library)], check=True)
        record.write_text(source_hash, encoding='ascii', newline='\n')
    return library


def mixer_api(path):
    api = C.CDLL(str(path))
    api.bh_mix_create.restype = C.c_void_p
    api.bh_mix_free.argtypes = [C.c_void_p]
    api.bh_mix_add.argtypes = [C.c_void_p, C.c_uint, C.c_uint]
    api.bh_mix_continuity.argtypes = [C.c_void_p, C.c_int, C.c_int, C.c_uint, C.c_uint]
    api.bh_mix_timeline.argtypes = [C.c_void_p, C.c_int, C.c_int]
    api.bh_mix_write.argtypes = [C.c_void_p, C.c_int, C.c_void_p, C.c_uint]
    api.bh_mix_render.argtypes = [C.c_void_p, C.c_void_p, C.c_uint, C.c_float]
    return api


def unity_measure(api):
    mixer = api.bh_mix_create()
    try:
        source = api.bh_mix_add(mixer, 0, 48000)
        assert api.bh_mix_continuity(mixer, source, 1, 0, 0) == 0
        assert api.bh_mix_timeline(mixer, source, 1) == 0
        # Independent channels include low-level 24-bit steps and transients.
        rng = np.random.default_rng(20261212)
        pcm = (rng.integers(-2**21, 2**21, size=(24000, 2)) / 2**23).astype('f4')
        pcm[4000:8000] *= np.float32(2**-14)
        pcm[12000] = [.999, -.731]
        api.bh_mix_write(mixer, source, pcm.ctypes.data, len(pcm))
        output = np.empty_like(pcm)
        offset = block = 0
        sizes = (1, 17, 127, 480, 511, 960)
        while offset < len(output):
            n = min(sizes[block % len(sizes)], len(output) - offset)
            assert api.bh_mix_render(mixer, output[offset:].ctypes.data, n, 1) == 1
            offset += n
            block += 1
        best = None
        for delay in range(129):
            reference = pcm[1500:22000].astype('f8')
            actual = output[1500 + delay:22000 + delay].astype('f8')
            error = float(np.max(abs(reference - actual)))
            if best is None or error < best['maximum_absolute_error']:
                rms = float(np.sqrt(np.mean((reference - actual)**2)))
                best = dict(delay_frames=delay, maximum_absolute_error=error, rms_error=rms,
                            exactly_equal_samples=int(np.count_nonzero(reference == actual)), samples=reference.size)
        return best
    finally:
        api.bh_mix_free(mixer)


def fractional_measure(api):
    api.bh_test_fractional_response.argtypes = [C.c_void_p, C.c_double, C.c_double,
                                              C.POINTER(C.c_double), C.POINTER(C.c_double)]
    mixer = api.bh_mix_create()
    result = []
    try:
        for frequency in (0, 100, 997, 5000, 10000, 16000, 20000, 22000, 23500):
            gain, error = [], []
            for fraction in np.linspace(0, 1, 257):
                real, imaginary = C.c_double(), C.c_double()
                assert api.bh_test_fractional_response(mixer, frequency / 48000, fraction,
                                                      C.byref(real), C.byref(imaginary)) == 0
                actual = complex(real.value, imaginary.value)
                expected = np.exp(2j * np.pi * frequency / 48000 * (192 + fraction))
                gain.append(20 * math.log10(max(abs(actual), 1e-30)))
                error.append(abs(actual - expected))
            result.append(dict(frequency_hz=frequency, minimum_gain_db=min(gain), maximum_gain_db=max(gain),
                               maximum_complex_error=max(error), rms_complex_error=float(np.sqrt(np.mean(np.square(error))))))
        return result
    finally:
        api.bh_mix_free(mixer)


def ldac_measure():
    from bluehead.ldac import _NativeMedia
    from tests.vectors import LdacEncoder, rtp
    rate = 96000
    time = np.arange(rate // 3) / rate
    tones = (997, 13001, 30017, 40009)
    left = sum(.08 * np.sin(2*np.pi*f*time) for f in tones)
    right = sum(.07 * np.cos(2*np.pi*f*time + .2) for f in tones)
    encoder = LdacEncoder(rate, 0)
    try:
        packets = [rtp(data, i) for i, data in enumerate(encoder.encode(np.column_stack((left, right)).astype('f4')))]
    finally:
        encoder.close()
    outputs = {}
    for output_rate in (48000, 96000):
        decoder = _NativeMedia(rate, output_rate)
        blocks = []
        try:
            for packet in packets:
                assert decoder.decode_packet(packet) >= 0
                blocks.append(np.frombuffer(decoder.read_pcm(), 'f4').reshape(-1, 2).copy())
            pcm = np.concatenate(blocks)[output_rate // 20:]
            t = np.arange(len(pcm)) / output_rate
            # Fit each tone independently after decoder/SRC startup. Reference
            # is the same codec decoded at 96k, not the original lossless file.
            amplitudes = {}
            basis = np.column_stack([wave(2*np.pi*f*t) for f in tones for wave in (np.sin, np.cos)]
                                    + [np.ones(len(t))])
            coeff = np.linalg.lstsq(basis, pcm[:, 0].astype('f8'), rcond=None)[0]
            for i, f in enumerate(tones):
                amplitudes[str(f)] = float(np.hypot(coeff[i*2], coeff[i*2+1]))
            outputs[str(output_rate)] = dict(frames=len(pcm), tone_amplitudes=amplitudes)
        finally:
            decoder.close()
    return dict(codec='LDAC high quality', source_rate=rate, decoded=outputs,
                note='Frequencies above 24kHz characterize retained bandwidth; they do not establish audibility.')


def audit():
    assets = ROOT / 'bluehead/assets'
    with os.add_dll_directory(str(assets)):
        production = mixer_api(assets / 'bluehead_audio.dll')
        probe = mixer_api(build_probe())
        return dict(scope='Synthetic in-memory audio and compiled production kernel; no radio, audio device or recording',
                    source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                    native_sha256={name: digest(assets / name) for name in ('bluehead_audio.dll', 'bluehead_ldac_media.dll', 'bluehead_voice.dll')},
                    unity=unity_measure(production), fractional_delay=fractional_measure(probe), ldac_96k=ldac_measure())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    arguments = parser.parse_args()
    result = audit()
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps({'report': str(arguments.report), 'unity': result['unity'],
                      'maximum_error_below_20khz': max(row['maximum_complex_error'] for row in result['fractional_delay']
                                                     if row['frequency_hz'] <= 20000),
                      'ldac_96k': result['ldac_96k']}, ensure_ascii=False))

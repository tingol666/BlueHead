"""Inverse quantization against the pinned AOSP encoder, not a second decoder."""
import ctypes as C
from pathlib import Path
import subprocess

import numpy as np
import pytest

from tools.native_build import compiler

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def quantizer(tmp_path_factory):
    folder = tmp_path_factory.mktemp('ldac-quantization')
    library = folder / 'quantization.dll'
    decoder = ROOT / 'vendor/libldacdec'
    encoder = ROOT / 'vendor/libldac_encoder'
    sources = ['bridge.c', 'libldacdec.c', 'bit_allocation.c', 'huffCodes.c',
               'bit_reader.c', 'utility.c', 'imdct.c', 'spectrum.c']
    subprocess.run([compiler(), '-shared', '-std=gnu11', '-O2', '-static', '-static-libgcc',
                    '-Wl,--no-insert-timestamp', '-I', str(encoder / 'inc'),
                    '-I', str(encoder / 'src'), str(ROOT / 'tests/native_ldac_quantizer.c'),
                    str(ROOT / 'tests/native_ldac_spectrum.c'),
                    *[str(decoder / name) for name in sources], '-o', str(library), '-lm'],
                   check=True, timeout=60)
    api = C.CDLL(str(library))
    api.bh_test_ldac_quantize.argtypes = [C.c_int, C.c_void_p, C.c_void_p, C.c_void_p]
    api.bh_test_ldac_dequantize.argtypes = [C.c_int, C.c_void_p, C.c_void_p, C.c_void_p]
    return api


@pytest.mark.parametrize('fine', range(1, 16))
def test_fine_spectrum_restores_the_encoders_residual_at_its_transmitted_precision(quantizer, fine):
    # Values span signs and fractional coarse bins; no transform or alignment
    # error can hide a wrongly scaled residual in this unit-level oracle.
    rng = np.random.default_rng(214)
    errors, coarse_errors = [], []
    for samples in rng.uniform(-.95, .95, (100, 2)).astype('f4'):
        coarse, residual = np.empty(2, 'i4'), np.empty(2, 'i4')
        output = np.empty(2, 'f4')
        assert quantizer.bh_test_ldac_quantize(fine, samples.ctypes.data,
                                              coarse.ctypes.data, residual.ctypes.data) == 0
        quantizer.bh_test_ldac_dequantize(fine, coarse.ctypes.data,
                                         residual.ctypes.data, output.ctypes.data)
        error = abs(output.astype('f8') - samples)
        coarse_error = abs(coarse.astype('f8') * (2 / 65535) - samples)
        # Sony's quantizer deliberately leaves 1/256 residual headroom.
        # Allow that small bias plus half a residual bin and float rounding.
        bound = (2 / ((2 ** (fine + 1) - 1) * 65535)) * .51 + coarse_error / 256 + 1.2e-7
        assert np.all(error <= bound), (fine, float(max(error)), float(max(bound)))
        errors.extend(error)
        coarse_errors.extend(coarse_error)
    assert np.mean(np.square(errors)) < np.mean(np.square(coarse_errors)) * .3

"""Bounded asynchronous-conversion cost, with generated PCM and no endpoints.

--baseline may point at an earlier release DLL. Reports identify every binary.
Times include the small ctypes call overhead and OS scheduling; no conclusion
about Bluetooth end-to-end latency can be drawn from these measurements.
"""
import argparse
import ctypes as C
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.audio_fidelity_audit import mixer_api  # noqa: E402


def measure(path, source_rate, output_rate, sources, cycles=1800):
    api = mixer_api(path)
    for name, args, result in (
        ('bh_mix_value', [C.c_void_p, C.c_int, C.c_int], C.c_uint64),
        ('bh_mix_observe', [C.c_void_p, C.c_int, C.c_uint64, C.c_uint64], None),
        ('bh_render_create', [C.c_void_p, C.c_uint32], C.c_void_p),
        ('bh_render_process', [C.c_void_p, C.c_void_p, C.c_uint32], C.c_int),
        ('bh_render_free', [C.c_void_p], None),
    ):
        method = getattr(api, name)
        method.argtypes, method.restype = args, result
    mixer, renderer = api.bh_mix_create(), None
    try:
        quantum = output_rate // 100
        source_quantum = source_rate // 100
        data = np.full((source_rate // 10, 2), .03, 'f4')
        output = np.empty((quantum, 2), 'f4')
        indexes = [api.bh_mix_add(mixer, 1440, 8640) for _ in range(sources)]
        for index in indexes:
            assert index >= 0
            if source_rate != 48000:
                api.bh_mix_format.argtypes = [C.c_void_p, C.c_int, C.c_uint]
                assert api.bh_mix_format(mixer, index, source_rate) == 0
            assert api.bh_mix_continuity(mixer, index, 1, 1440, 5760) == 0
            assert api.bh_mix_timeline(mixer, index, 1) == 0
            api.bh_mix_write(mixer, index, data.ctypes.data, round(source_rate*.04))
        renderer = api.bh_render_create(mixer, output_rate)
        assert renderer
        counter, frequency = C.c_int64(), C.c_int64()
        C.windll.kernel32.QueryPerformanceFrequency(C.byref(frequency))
        C.windll.kernel32.QueryPerformanceCounter(C.byref(counter))
        origin = counter.value
        position = source_rate // 25
        costs = []
        fraction = 0.
        for cycle in range(cycles):
            fraction += source_quantum*1.0002
            supply = math.floor(fraction)
            fraction -= supply
            for index in indexes:
                arrival = origin + round(cycle*.01*frequency.value)
                api.bh_mix_observe(mixer, index, round(position*48000/source_rate), arrival)
                api.bh_mix_write(mixer, index, data.ctypes.data, supply)
            position += supply
            start = time.perf_counter_ns()
            assert api.bh_render_process(renderer, output.ctypes.data, quantum) >= 0
            duration = (time.perf_counter_ns()-start)/1000
            if cycle > 300:
                costs.append(duration)
        return dict(source_rate=source_rate, output_rate=output_rate, sources=sources,
                    callback_ms=10, correction_ppm=200,
                    duration_us=dict(zip(('median', 'p95', 'p99', 'max'),
                                         map(float, np.percentile(costs, [50, 95, 99, 100])))),
                    dropped_frames=sum(api.bh_mix_value(mixer, i, 1) for i in indexes),
                    underruns=sum(api.bh_mix_value(mixer, i, 3) for i in indexes),
                    callback_period_searches=sum(api.bh_mix_value(mixer, i, 17) for i in indexes))
    finally:
        if renderer:
            api.bh_render_free(renderer)
        api.bh_mix_free(mixer)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    runs = []
    paths = [('current', ROOT/'bluehead/assets/bluehead_audio.dll')]
    if args.baseline:
        paths.insert(0, ('baseline', args.baseline.resolve()))
    for name, path in paths:
        with os.add_dll_directory(str(path.parent)), os.add_dll_directory(str(path.parent.parent.parent)):
            cases = [(48000, 48000, n) for n in (1, 2, 8)]
            if name == 'current':
                cases += [(96000, 96000, 2), (96000, 48000, 2), (192000, 96000, 2)]
            runs.append(dict(binary=name, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                             cases=[measure(path, *case) for case in cases]))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(dict(scope=__doc__, runs=runs), indent=2)+'\n', encoding='utf-8')
    print(json.dumps(runs))


if __name__ == '__main__':
    main()

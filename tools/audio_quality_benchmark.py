"""Measure C mixing cost, including eight simultaneous presentation misses.

Generated PCM only; never opens USB, speakers or microphones. Measurements
include Python-to-C overhead and are not Bluetooth end-to-end latency.
"""
import argparse
import ctypes as C
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bluehead.pcm import NativeMixer  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--generic', action='store_true', help='Compare the former generic PLC callback')
    parser.add_argument('--amplitude', type=float, default=.06, help='Include subnormal/near-silence cost checks')
    args = parser.parse_args()
    measurements = []
    for sources, gap in ((1, False), (2, False), (8, False), (8, True)):
        mixer = NativeMixer()
        api = mixer.library
        api.bh_mix_continuity.argtypes = [C.c_void_p, C.c_int, C.c_int, C.c_uint, C.c_uint]
        api.bh_mix_timeline.argtypes = [C.c_void_p, C.c_int, C.c_int]
        streams = [mixer.add(0, 8640) for _ in range(sources)]
        for stream in streams:
            api.bh_mix_continuity(mixer.handle, stream.index, 1, 0, 0)
            api.bh_mix_timeline(mixer.handle, stream.index, 0 if args.generic else 1)
        t = np.arange(480) / 48000
        tone = (args.amplitude*np.sin(2*np.pi*1000*t)).astype('f4')
        pcm = np.column_stack((tone, -.37*tone)).astype('f4')
        output = np.empty((480, 2), 'f4')
        costs = []
        try:
            for cycle in range(2200):
                # Every 20 callbacks: refill history, then one real short gap.
                supply = not gap or cycle % 20 != 19
                for stream in streams:
                    if supply:
                        stream.write(pcm)
                before = time.perf_counter_ns()
                mixer.render_into(output, 1)
                elapsed = (time.perf_counter_ns() - before) / 1000
                if cycle >= 200 and (not gap or not supply):
                    costs.append(elapsed)
                if not np.isfinite(output).all():
                    raise ValueError('Non-finite native output')
            searches = sum(api.bh_mix_value(mixer.handle, stream.index, 17) for stream in streams)
            measurements.append({'sources': sources, 'first_gap_callback': gap, 'frames': 480,
                                 'callback_period_searches': searches,
                                 'duration_us': dict(zip(('median', 'p95', 'p99', 'max'),
                                                        map(float, np.percentile(costs, [50, 95, 99, 100]))))})
        finally:
            mixer.close()
    result = {'scope': __doc__, 'amplitude': args.amplitude, 'generic_gap_owner': args.generic, 'audio_sha256': hashlib.sha256(
        (ROOT / 'bluehead/assets/bluehead_audio.dll').read_bytes()).hexdigest(), 'measurements': measurements}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + '\n', 'utf-8')
    print(json.dumps(measurements))


if __name__ == '__main__':
    main()

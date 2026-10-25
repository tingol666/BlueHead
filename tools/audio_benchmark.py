"""Compare render cores and prefill, not Bluetooth end-to-end latency."""
import json
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bluehead.audio import Mixer


def measure(native, sources):
    mixer = Mixer(buffer_ms=0, native=native)
    output = np.empty((480, 2), np.float32)
    block = np.repeat((.05*np.sin(2*np.pi*1000*np.arange(480)/48000))[:, None], 2, axis=1).astype(np.float32)
    for index in range(sources):
        mixer.add(str(index))
    timings = []
    started = time.process_time()
    for _ in range(40000):
        for index in range(sources):
            mixer.write(str(index), block)
        before = time.perf_counter_ns()
        mixer.render_into(output)
        timings.append((time.perf_counter_ns() - before) / 1000)
    return {'sources': sources, 'native': native, 'render_median_us': float(np.median(timings)),
            'render_p95_us': float(np.percentile(timings, 95)), 'render_p99_us': float(np.percentile(timings, 99)),
            'iterations': 40000, 'cpu_seconds_for_400_seconds_pcm': time.process_time() - started}


if __name__ == '__main__':
    result = {'scope': 'Synthetic 48 kHz PCM render-core benchmark. No radio or physical audio; not total application CPU or end-to-end delay.',
              'runs': [measure(native, sources) for sources in (1, 2, 4) for native in (False, True)],
              'prefill_configuration': {'previous_shared_ms': 60, 'new_call_ms': 30, 'new_media_ms': 60}}
    path = Path(sys.argv[1] if len(sys.argv) > 1 else 'work/audio-benchmark-0.2.json')
    path.write_text(json.dumps(result, indent=2) + '\n', 'utf-8')
    print(json.dumps(result, indent=2))

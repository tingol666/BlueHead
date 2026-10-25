"""Offline C-callback cost and fallback quality/latency; no audio devices used."""
import json
from pathlib import Path
import sys
import time

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bluehead.audio import Mixer
from bluehead.realtime import NativeCapture, NativeRender


def main():
    measurements = []
    for rate in (44100, 48000, 88200, 96000, 192000):
        mixer = Mixer(buffer_ms=60)
        mixer.add("generated")
        renderer = NativeRender(mixer.native, rate)
        output = np.empty((rate//100, 2), np.float32)
        source = np.full((480, 2), .125, np.float32)
        mixer.write("generated", np.full((2880, 2), .125, np.float32))
        costs = []
        for i in range(2000):
            mixer.write("generated", source)
            before = time.perf_counter_ns()
            renderer.api.bh_output_callback(None, output.ctypes.data, len(output), None, 0, renderer.handle)
            elapsed = (time.perf_counter_ns()-before)/1000
            if i >= 100:
                costs.append(elapsed)
        measurements.append({"direction": "output", "sample_rate": rate, "block_ms": 10,
                             "duration_us": dict(zip(("median", "p95", "p99", "max"),
                                                     map(float, np.percentile(costs, [50, 95, 99, 100])))),
                             "filter_latency_frames": renderer.snapshot()["filter_latency_frames"],
                             "native_counters": renderer.snapshot(), "mixer": mixer.snapshot()})
        renderer.close()
    for rate in (8000, 16000):
        capture = NativeCapture(48000, 2, rate)
        source = np.full((480, 2), .125, np.float32)
        output = np.empty(1024, np.int16)
        costs = []
        for i in range(2000):
            before = time.perf_counter_ns()
            capture.api.bh_input_callback(source.ctypes.data, None, len(source), None, 0, capture.handle)
            costs.append((time.perf_counter_ns()-before)/1000)
            capture.read_into(output)
        measurements.append({"direction": "input_callback", "sample_rate": 48000, "voice_rate": rate,
                             "block_ms": 10, "duration_us": dict(zip(("median", "p95", "p99", "max"),
                                                     map(float, np.percentile(costs[100:], [50, 95, 99, 100])))),
                             "native_counters": capture.snapshot()})
        capture.close()
    result = {"scope": "Offline generated PCM; includes Python-to-C invocation overhead; not end-to-end latency",
              "measurements": measurements}
    Path(sys.argv[1]).write_text(json.dumps(result, indent=2), "utf-8")


if __name__ == "__main__":
    main()

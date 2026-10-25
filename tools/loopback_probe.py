"""Two low-level test tones verify process-tree exclusion. No audio files saved."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bluehead.audio import resolve_endpoint  # noqa: E402
from bluehead.loopback import ProcessCapture  # noqa: E402
from bluehead.audio_runtime import on_audio_thread  # noqa: E402


@on_audio_thread
def tone(frequency):
    selected = resolve_endpoint(None, "output")
    rate = int(sd.query_devices(selected, "output")["default_samplerate"])
    position = [0]
    def callback(out, count, timing, status):
        phase = np.arange(position[0], position[0] + count) * (2 * np.pi * frequency / rate)
        out[:] = (.012 * np.sin(phase)).reshape(-1, 1)
        position[0] += count
    stream = sd.OutputStream(device=selected, samplerate=rate, channels=2, dtype="float32", callback=callback)
    stream.start()
    return stream


def receive(capture, seconds=.8):
    chunks = []
    buffer = np.empty(480, np.float32)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        count = capture.read(buffer)
        if count:
            chunks.append(buffer[:count].copy())
        else:
            time.sleep(.003)
    return np.concatenate(chunks) if chunks else np.zeros(480, np.float32)


def magnitude(data, frequency):
    spectrum = np.abs(np.fft.rfft(data * np.hanning(len(data)))) * 4 / len(data)
    bins = np.fft.rfftfreq(len(data), 1 / 48000)
    return float(np.max(spectrum[np.abs(bins - frequency) < 12], initial=0))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        stream = tone(1429)
        print("ready", flush=True)
        sys.stdin.read(1)
        on_audio_thread(stream.close)()
        raise SystemExit(0)
    # Bypass the venv launcher, which can reparent its child to its own launcher.
    child = subprocess.Popen([sys._base_executable, str(Path(__file__).resolve()), "--child"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                             creationflags=subprocess.CREATE_NO_WINDOW,
                             env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)))
    stream = capture = None
    try:
        assert child.stdout.readline().strip() == "ready"
        capture = ProcessCapture(excluded_pid=child.pid)
        stream = tone(941)
        samples = receive(capture)
        included, excluded = magnitude(samples, 941), magnitude(samples, 1429)
        assert included > .003 and excluded < included * .05, (included, excluded)
        capture.close()
        capture = ProcessCapture()  # Exclude the current worker AND its child.
        samples = receive(capture)
        self_level, child_level = magnitude(samples, 941), magnitude(samples, 1429)
        assert max(self_level, child_level) < included * .05, (self_level, child_level)
        report = {"passed": True, "included_signal": included, "excluded_signal": excluded,
                  "excluded_self": self_level, "excluded_child": child_level,
                  "scope": "Real WASAPI process-loopback capture, inclusion vs exclusion of parent/child tones; no recording files"}
        Path(sys.argv[1]).write_text(json.dumps(report, indent=2), "utf-8")
        print(json.dumps(report))
    finally:
        if capture:
            capture.close()
        if stream:
            on_audio_thread(stream.close)()
        child.stdin.write("x")
        child.stdin.flush()
        child.wait(3)

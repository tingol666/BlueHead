"""Explicit native WASAPI process-exclusion probe; never runs on import.

With --run, two processes play quiet generated tones and briefly capture the
included computer audio in memory. Only numeric levels are saved; no microphone,
Bluetooth adapter, driver or audio recording file is used. Pause other playback.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Tone:
    """Development tone generation; actual rendering stays in the native callback."""
    def __init__(self, frequency):
        import numpy as np
        from bluehead.audio import AudioOutput, Mixer
        self.np, self.frequency = np, frequency
        self.position = 0
        self.stop = threading.Event()
        self.error = None
        self.thread = None
        self.output = None
        self.mixer = Mixer(buffer_ms=30, max_ms=120)
        self.mixer.add("generated-probe")
        try:
            self._write(1440)
            self.output = AudioOutput(self.mixer)
            self.output.start()
            self.thread = threading.Thread(target=self._generate, name="generated-probe", daemon=True)
            self.thread.start()
        except BaseException:
            self.close()
            raise

    def _write(self, frames):
        np = self.np
        phase = np.arange(self.position, self.position + frames) * (2 * np.pi * self.frequency / 48000)
        self.position += frames
        mono = (.012 * np.sin(phase)).astype(np.float32)
        stereo = np.repeat(mono[:, None], 2, axis=1)
        self.mixer.write_float_bytes("generated-probe", stereo.tobytes())

    def _generate(self):
        deadline = time.monotonic()
        try:
            while not self.stop.is_set():
                self._write(480)
                deadline += .01
                now = time.monotonic()
                if now - deadline > .1:
                    deadline = now
                self.stop.wait(max(0., deadline - now))
        except Exception as error:
            self.error = type(error).__name__
            self.stop.set()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(1)
            if self.thread.is_alive():
                raise RuntimeError("generated-tone worker has not stopped")
        if self.output:
            self.output.close()
            self.output = None
        if getattr(self.mixer, "native", None):
            self.mixer.native.close()


def receive(capture, seconds):
    import numpy as np
    chunks = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        data = capture.read_bytes(4800)
        if data:
            chunks.append(data)
        else:
            time.sleep(.003)
    return np.frombuffer(b"".join(chunks), np.float32) if chunks else np.zeros(480, np.float32)


def magnitude(data, frequency):
    import numpy as np
    spectrum = np.abs(np.fft.rfft(data * np.hanning(len(data)))) * 4 / len(data)
    bins = np.fft.rfftfreq(len(data), 1 / 48000)
    return float(np.max(spectrum[np.abs(bins - frequency) < 12], initial=0))


def tone_child():
    # This private mode is started only by the explicit --run parent.
    stream = Tone(1429)
    finished = threading.Event()
    def wait_for_parent():
        sys.stdin.read(1)
        finished.set()
    threading.Thread(target=wait_for_parent, daemon=True, name="probe-parent").start()
    try:
        print("ready", flush=True)
        finished.wait(10)
    finally:
        stream.close()


def wait_ready(child):
    ready, text = threading.Event(), []
    def read_line():
        text.append(child.stdout.readline().strip())
        ready.set()
    threading.Thread(target=read_line, name="probe-startup", daemon=True).start()
    if not ready.wait(5) or text != ["ready"]:
        raise RuntimeError("generated-tone child did not start")


def probe(seconds):
    from bluehead.loopback import ProcessCapture, supported
    report = {"passed": False,
              "scope": "Native WASAPI process exclusion; generated parent/child tones; only numeric levels saved",
              "microphone_opened": False, "bluetooth_opened": False}
    child = stream = capture = None
    cleanup_errors = []
    try:
        if not supported():
            raise OSError("Process exclusion requires Windows build 20348 or later")
        # Use the real interpreter, avoiding an extra venv launcher in the tree.
        child = subprocess.Popen([sys._base_executable, str(Path(__file__).resolve()), "--tone-child"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)))
        wait_ready(child)
        stream = Tone(941)
        time.sleep(.2)
        capture = ProcessCapture(excluded_pid=child.pid)
        samples = receive(capture, seconds)
        included, excluded = magnitude(samples, 941), magnitude(samples, 1429)
        if stream.error or included <= .003 or excluded >= included * .05:
            raise RuntimeError("parent inclusion or child exclusion failed")
        capture.close()
        capture = ProcessCapture()
        samples = receive(capture, seconds)
        self_level, child_level = magnitude(samples, 941), magnitude(samples, 1429)
        if stream.error or max(self_level, child_level) >= included * .05:
            raise RuntimeError("self-tree exclusion failed")
        report.update(passed=True, included_signal=included, excluded_signal=excluded,
                      excluded_self=self_level, excluded_child=child_level)
    except Exception as error:
        report["error_type"] = type(error).__name__
        report["message"] = "Check native dependencies, supported Windows build, default output, and other playing applications."
    finally:
        for resource in (capture, stream):
            if resource:
                try:
                    resource.close()
                except Exception as error:
                    cleanup_errors.append(type(error).__name__)
        if child:
            with contextlib.suppress(BrokenPipeError, OSError):
                child.stdin.write("x")
                child.stdin.flush()
            try:
                child.wait(3)
            except subprocess.TimeoutExpired:
                child.terminate()
                try:
                    child.wait(2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(1)
            child.stdin.close()
            child.stdout.close()
        if cleanup_errors:
            report.update(passed=False, cleanup_errors=cleanup_errors)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="explicitly play generated tones and capture computer audio")
    parser.add_argument("--report", type=Path, help="optional numeric JSON report")
    parser.add_argument("--seconds", type=float, default=1.2, help="measurement duration per phase, 0.8..3 seconds")
    parser.add_argument("--tone-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.tone_child:
        tone_child()
        return 0
    if not args.run:
        parser.error("--run is required; this is a physical audio probe")
    if not .8 <= args.seconds <= 3:
        parser.error("seconds must be between 0.8 and 3")
    report = probe(args.seconds)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", "utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

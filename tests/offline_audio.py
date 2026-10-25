"""Native device callback harness; every endpoint is in-memory Context._offline.

Only endpoint selection/opening is substituted. PCM rendering, capture rings,
sample-rate conversion, counters, stop/reset and stream destruction are real C.
"""

from __future__ import annotations
import ctypes as c

from bluehead import audio_device as device


class OfflineEndpoints:
    def __init__(self, monkeypatch, rates=(48000,), *, shared=True):
        self.owner = device.Context()
        self.rates = tuple(rates)
        self.shared = shared
        self.opened = []
        self.active = {device.PLAYBACK: set(), device.CAPTURE: set()}
        self.fail_open = set()
        self.fail_start = set()
        monkeypatch.setattr(device, "context", lambda: self)
        monkeypatch.setattr("bluehead.capture.context", lambda: self)
        monkeypatch.setattr("bluehead.audio.resolve_endpoint", lambda selection, direction: selection)

    def _selected(self, selected):
        return 0 if selected is None else int(str(selected).removeprefix("endpoint-"))

    def _created(self, direction, selected, mixer=None, output_rate=48000):
        index = self._selected(selected)
        if index in self.fail_open:
            raise OSError("endpoint open failed")
        rate = 48000 if direction == device.PLAYBACK and self.shared else self.rates[index]
        stream = self.owner._offline(
            direction,
            mixer=mixer,
            rate=rate,
            channels=2 if direction == device.PLAYBACK else 1,
            output_rate=output_rate,
        )
        stream.device = selected
        stream.endpoint_id = f"endpoint-{index}" if selected is not None else None
        original_start, original_stop, original_close = stream.start, stream.stop, stream.close

        def start():
            if index in self.fail_start:
                raise OSError("endpoint start failed")
            assert not self.active[direction], "Two native streams could consume one audio queue"
            original_start()
            self.active[direction].add(stream)
            return stream

        def stop():
            original_stop()
            self.active[direction].discard(stream)
            return stream

        def close():
            original_close()
            self.active[direction].discard(stream)

        stream.start, stream.stop, stream.close = start, stop, close
        self.opened.append(stream)
        return stream

    def create_output(self, mixer, selected=None):
        return self._created(device.PLAYBACK, selected, mixer)

    def create_capture(self, selected=None, *, output_rate=48000):
        return self._created(device.CAPTURE, selected, output_rate=output_rate)

    def process(self, stream, output=None, input=None, frames=None):
        if frames is None:
            frames = len(output) if output is not None else len(input)
        with stream._lock:
            assert not stream.closed
            result = self.owner.api.bh_device_offline_process(
                stream.handle,
                input.ctypes.data if input is not None else None,
                output.ctypes.data if output is not None else None,
                frames,
            )
            if result:
                raise OSError(f"Offline PCM processing failed: {result}")

    def close(self):
        self.owner.close()

    def snapshot_structure(self, stream):
        record = device.DeviceStats(size=c.sizeof(device.DeviceStats))
        assert self.owner.api.bh_device_stats(stream.handle, c.byref(record)) == 0
        return record

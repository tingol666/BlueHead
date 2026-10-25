"""Stateful decoders and bounded multi-device PCM mixer; no recording."""
from collections import deque
from dataclasses import dataclass
import contextlib
import logging
import threading

import math

from .protocol import Format
from .audio_runtime import on_audio_thread, register, unregister, close_raw, commit_change

log = logging.getLogger(__name__)


@on_audio_thread
def resolve_endpoint(device, direction):
    from .audio_device import inventory
    from .endpoints import portaudio_id
    if device is None or isinstance(device, str):
        return device
    identity = portaudio_id(device)
    for item in inventory():
        if item["endpoint_id"] == identity and item["direction"] == direction:
            return item["endpoint_id"]
    raise ValueError("所选音频设备已断开，请重新选择")


def Decoder(fmt: Format, output_rate=48000):
    if fmt.codec == "ldac":
        from .ldac import IsolatedMedia
        return IsolatedMedia(fmt, output_rate)
    from .media import NativeMedia
    return NativeMedia(fmt, output_rate)


@dataclass
class MixInput:
    chunks: deque
    frames: int = 0
    gain: float = 1.0
    muted: bool = False
    dropped: int = 0
    buffering: bool = True
    gate: float = 1.0
    gate_target: float = 1.0


class Mixer:
    def __init__(self, rate=48000, buffer_ms=60, max_ms=180, call_buffer_ms=30, native=True):
        self.rate = rate
        self.target = int(rate * buffer_ms / 1000)
        self.maximum = int(rate * max_ms / 1000)
        self.call_target = int(rate * min(buffer_ms, call_buffer_ms) / 1000)
        self.call_maximum = min(self.maximum, int(rate * max(60, call_buffer_ms * 3) / 1000))
        self.native = None
        if native:
            if rate != 48000:
                raise ValueError("Native mixer uses a fixed 48 kHz PCM clock")
            from .pcm import NativeMixer
            self.native = NativeMixer()
        self.inputs: dict[str, MixInput] = {}
        self.gains = {}
        self.lock = threading.Lock()
        self.master = 1.0
        self.call_active = False
        self.call_source = None
        self.underruns = 0

    @property
    def master(self):
        return self._master

    @master.setter
    def master(self, value):
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("Audio gain must be finite")
        self._master = max(0.0, min(2.0, value))
        if self.native:
            self.native.library.bh_mix_master(self.native.handle, self._master)

    @property
    def call_active(self):
        return self._call_active

    @call_active.setter
    def call_active(self, value):
        self._call_active = value
        self._update_native_call()

    @property
    def call_source(self):
        return self._call_source

    @call_source.setter
    def call_source(self, value):
        self._call_source = value
        self._update_native_call()

    def _update_native_call(self):
        if self.native:
            stream = self.inputs.get(getattr(self, "_call_source", None)) if getattr(self, "_call_active", False) else None
            self.native.library.bh_mix_call(self.native.handle, stream.index if stream else -1)

    def add(self, key):
        with self.lock:
            if self.native:
                if key not in self.inputs:
                    target = self.call_target if key.startswith("call:") else self.target
                    maximum = self.call_maximum if key.startswith("call:") else self.maximum
                    self.inputs[key] = self.native.add(target, maximum)
                    stream = self.inputs[key]
                    stream.gain = self.gains.get(key, 1.0)
                    self.native.library.bh_mix_gain(self.native.handle, stream.index, stream.gain)
                return
            self.inputs.setdefault(key, MixInput(deque(), gain=self.gains.get(key, 1.0)))

    def remove(self, key):
        with self.lock:
            stream = self.inputs.pop(key, None)
            if self.native and stream:
                self.native.library.bh_mix_remove(self.native.handle, stream.index)

    def clear(self, key):
        with self.lock:
            stream = self.inputs.get(key)
            if stream:
                if self.native:
                    self.native.library.bh_mix_clear(self.native.handle, stream.index)
                    return
                stream.chunks.clear()
                stream.frames, stream.buffering = 0, True

    def set_gain(self, key, value):
        with self.lock:
            self.gains[key] = max(0.0, min(2.0, float(value)))
            if key in self.inputs:
                self.inputs[key].gain = self.gains[key]
                if self.native:
                    stream = self.inputs[key]
                    self.native.library.bh_mix_gain(self.native.handle, stream.index, stream.gain)

    def set_enabled(self, key, enabled):
        with self.lock:
            stream = self.inputs.get(key)
            if stream:
                if self.native:
                    self.native.library.bh_mix_gate(self.native.handle, stream.index, int(enabled))
                else:
                    stream.gate_target = float(enabled)
                    if not stream.frames or stream.buffering:
                        stream.gate = stream.gate_target

    def write(self, key, samples):
        import numpy as np  # Development/reference array API only.
        if samples.ndim != 2 or samples.shape[1] != 2:
            raise ValueError("Mixer requires interleaved stereo")
        with self.lock:
            stream = self.inputs.get(key)
            if stream is None:
                return
            if self.native:
                stream.write(samples)
                return
            # Bounded latency: drop oldest complete blocks, never grow without bound.
            samples = np.asarray(samples[-self.maximum:], dtype=np.float32)
            while stream.chunks and stream.frames + len(samples) > self.maximum:
                stream.frames -= len(stream.chunks.popleft())
                stream.dropped += 1
            stream.chunks.append(samples)
            stream.frames += len(samples)

    def write_voice(self, key, pcm):
        """48 kHz mono S16 from the native voice engine, with no array conversion."""
        if not isinstance(pcm, bytes) or len(pcm) % 2 or len(pcm) > 48064 * 2:
            raise ValueError("Invalid native voice PCM")
        with self.lock:
            stream = self.inputs.get(key)
            if stream is None:
                return
            api = self.native.library
            api.bh_mix_write_s16_mono(self.native.handle, stream.index, pcm, len(pcm) // 2)

    def write_float_bytes(self, key, pcm):
        """Owned stereo float32 bytes from an isolated native decoder."""
        if not isinstance(pcm, bytes) or len(pcm) % 8 or len(pcm) > 48064 * 8:
            raise ValueError("Invalid native stereo PCM")
        with self.lock:
            stream = self.inputs.get(key)
            if stream is not None:
                self.native.library.bh_mix_write(self.native.handle, stream.index, pcm, len(pcm) // 8)

    def render(self, frames):
        import numpy as np  # Development/offline facade; native callback bypasses it.
        if self.native:
            output = np.empty((frames, 2), dtype=np.float32)
            self.render_into(output)
            return output
        out = np.zeros((frames, 2), dtype=np.float32)
        with self.lock:
            for key, stream in self.inputs.items():
                if stream.buffering:
                    target = self.call_target if key.startswith("call:") else self.target
                    if stream.frames < target:
                        continue
                    stream.buffering = False
                offset = 0
                gain = 0.0 if stream.muted else stream.gain
                if self.call_active and key != self.call_source:
                    gain *= 0.15
                while offset < frames and stream.chunks:
                    chunk = stream.chunks[0]
                    take = min(frames - offset, len(chunk))
                    step = 1 / 480
                    envelope = stream.gate + np.arange(1, take + 1) * (step if stream.gate_target > stream.gate else -step)
                    envelope = np.clip(envelope, min(stream.gate, stream.gate_target), max(stream.gate, stream.gate_target))
                    stream.gate = float(envelope[-1])
                    out[offset:offset + take] += chunk[:take] * gain * envelope[:, None]
                    if take == len(chunk):
                        stream.chunks.popleft()
                    else:
                        stream.chunks[0] = chunk[take:]
                    stream.frames -= take
                    offset += take
                if offset < frames:
                    stream.buffering = True
                    self.underruns += 1
        out *= self.master
        # Apply gain only when needed; unchanged samples for normal single-source audio.
        peak = float(np.max(np.abs(out))) if out.size else 0.0
        if peak > 0.98:
            out *= 0.98 / peak
        return out

    def render_into(self, output):
        if self.native:
            self.native.render_into(output, self.master)
        else:
            output[:] = self.render(len(output))

    def snapshot(self):
        with self.lock:
            return {
                "native_mixer": self.native is not None,
                "media_buffer_ms": round(self.target * 1000 / self.rate),
                "call_buffer_ms": round(self.call_target * 1000 / self.rate),
                "underruns": int(self.native.library.bh_mix_value(self.native.handle, -1, 0)) if self.native else self.underruns,
                "callback_contentions": int(self.native.library.bh_mix_value(self.native.handle, -1, 1)) if self.native else 0,
                "streams": [{"queued_ms": round(s.frames * 1000 / self.rate, 2), "dropped_frames": int(s.dropped)}
                            for s in self.inputs.values()],
            }


class SwitchableStream:
    """Replace an audio endpoint without two callbacks consuming the same queue."""
    @on_audio_thread
    def __init__(self, factory, device, direction=None):
        self.direction = direction
        self._factory = factory
        self._lock = threading.RLock()
        self.device = device
        candidate = factory(device)
        try:
            commit_change()
        except Exception:
            close_raw(candidate)
            raise
        self.stream = candidate
        self.running = self.closed = self.closing = False
        if direction in ("input", "output"):
            register(self)

    @on_audio_thread
    def start(self):
        with self._lock:
            if self.closed or self.closing:
                raise RuntimeError("音频设备已经关闭")
            commit_change()
            self.stream.start()
            self.running = True

    @on_audio_thread
    def switch_device(self, device, force=False):
        with self._lock:
            if self.closed or self.closing:
                raise RuntimeError("音频连接已结束")
            if device == self.device and not force:
                return
            # Open the replacement before interrupting the working endpoint.
            candidate = self._factory(device)
            previous = self.stream
            previous_stopped = False
            try:
                commit_change()
                if self.running:
                    previous.stop()
                    previous_stopped = True
                    candidate.start()
            except Exception:
                with contextlib.suppress(Exception):
                    close_raw(candidate)
                if previous_stopped:
                    previous.start()
                raise
            self.stream, self.device = candidate, device
            try:
                close_raw(previous)
            except Exception:
                log.warning("Old audio endpoint did not close cleanly", exc_info=True)

    @on_audio_thread
    def close(self):
        with self._lock:
            if not self.closed:
                commit_change()
                self.closing = True
                unregister(self)
                close_raw(self.stream)
                self.closed, self.running = True, False
                unregister(self)


class OutputRenderer:
    """Offline facade for the same native renderer used by PortAudio."""
    def __init__(self, mixer, rate):
        from .realtime import NativeRender
        self.mixer, self.rate = mixer, rate
        self.native = NativeRender(mixer.native, rate)

    def render(self, count):
        import numpy as np
        out = np.empty((count, 2), dtype=np.float32)
        self.native.render_into(out)
        return out


class AudioOutput(SwitchableStream):
    @on_audio_thread
    def __init__(self, mixer, output_device=None):
        from .audio_device import context
        self.mixer = mixer

        def create(device):
            return context().create_output(mixer, resolve_endpoint(device, "output"))
        super().__init__(create, output_device, "output")

    @property
    def underflows(self):
        return self.stream.snapshot().get("status_events", 0)

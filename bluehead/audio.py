"""Stateful decoders and bounded multi-device PCM mixer; no recording."""
from collections import deque
from dataclasses import dataclass
import contextlib
import logging
import threading

import av
import numpy as np

from .protocol import Depacketizer, Format
from .audio_runtime import on_audio_thread, register, unregister, close_raw, commit_change

log = logging.getLogger(__name__)


@on_audio_thread
def resolve_endpoint(device, direction):
    """Prefer the Windows default WASAPI endpoint over PortAudio's MME default."""
    if device is not None:
        return device
    import sounddevice as sd
    for api in sd.query_hostapis():
        if "WASAPI" in api["name"]:
            selected = api.get("default_" + direction + "_device", -1)
            if selected is not None and selected >= 0:
                return int(selected)
    return None


class Decoder:
    def __init__(self, fmt: Format, output_rate: int = 48000):
        self.fmt, self.output_rate = fmt, output_rate
        self.depacketizer = Depacketizer(fmt)
        self.errors, self.frames, self.bytes = 0, 0, 0
        self.coded_bytes = self.recoveries = 0
        self.synchronizing = False
        self.sync_buffer = bytearray()
        self.resampler = av.AudioResampler(format="flt", layout="stereo", rate=output_rate)
        if fmt.codec == "ldac":
            from .ldac import LdacDecoder
            self.native = LdacDecoder(fmt.rate)
            self.context = None
        else:
            self.native = None
            self.context = self._context()

    def _context(self):
        context = av.CodecContext.create({"aac": "aac_latm"}.get(self.fmt.codec, self.fmt.codec), "r")
        context.sample_rate = self.fmt.rate
        context.layout = "mono" if self.fmt.channels == 1 else "stereo"
        context.open()
        return context

    def restart(self):
        # Resuming can reset RTP sequence and aptX predictor/parity state.
        # Keep AAC LATM StreamMuxConfig for useSameStreamMux packets.
        self.depacketizer = Depacketizer(self.fmt)
        self.synchronizing = False
        self.sync_buffer.clear()
        self.resampler = av.AudioResampler(format="flt", layout="stereo", rate=self.output_rate)
        if self.native:
            self.native.reset()
        elif self.fmt.codec != "aac":
            self.context = self._context()

    def _aptx_resync(self, chunk):
        """Find two parity cycles before restarting at a real 4-sample boundary.

        An ongoing encoder can resume with any of its eight sync phases. Resetting
        FFmpeg at every packet then makes all subsequent packets fail forever.
        The transmitted parity bit is bit 13 (aptX) / 19 (HD) of each channel's
        codeword; its stereo XOR is 00000001, repeated every eight blocks.
        """
        self.sync_buffer.extend(chunk)
        block = 6 if self.fmt.codec == "aptx_hd" else 4
        shift = 3 if block == 6 else 5
        data = bytes(self.sync_buffer)
        words = np.frombuffer(data, np.uint8).reshape(-1, block)
        parity = ((words[:, 0] ^ words[:, block // 2]) >> shift) & 1
        for start in (np.flatnonzero(parity == 1) - 7)[-16:][::-1]:
            if start < 0 or start + 16 > len(parity):
                continue
            if (parity[start:start + 7].any() or parity[start + 8:start + 15].any() or
                    parity[start + 15] != 1):
                continue
            self.context = self._context()
            self.resampler = av.AudioResampler(format="flt", layout="stereo", rate=self.output_rate)
            try:
                frames = self.context.decode(av.Packet(data[start * block:]))
            except av.InvalidDataError:
                continue
            self.sync_buffer.clear()
            self.synchronizing = False
            self.recoveries += 1
            return frames
        # Keep only enough trailing codewords to bridge the next packet.
        self.sync_buffer[:] = data[-32 * block:]
        return []

    def feed(self, data: bytes) -> list[np.ndarray]:
        self.bytes += len(data)
        result = []
        for chunk in self.depacketizer.feed(data):
            self.coded_bytes += len(chunk) - (3 if self.fmt.codec == "aac" else 0)
            if self.native:
                pcm = self.native.decode(chunk)
                frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), "flt", "stereo")
                frame.sample_rate = self.fmt.rate
                decoded = [frame]
            else:
                # SBC may contain multiple frames; its parser splits on frame boundaries.
                packets = self.context.parse(chunk) if self.fmt.codec == "sbc" else [av.Packet(chunk)]
                if self.synchronizing:
                    decoded = self._aptx_resync(chunk)
                else:
                    try:
                        decoded = [f for p in packets for f in self.context.decode(p)]
                    except av.InvalidDataError:
                        self.errors += 1
                        if self.fmt.codec not in ("aptx", "aptx_hd"):
                            raise
                        self.synchronizing = True
                        decoded = self._aptx_resync(chunk)
            for frame in decoded:
                if frame.sample_rate != self.fmt.rate:
                    raise ValueError("Decoded sample rate differs from negotiated rate")
                for out in self.resampler.resample(frame):
                    samples = out.to_ndarray().reshape(-1, 2).copy()
                    if not np.isfinite(samples).all():
                        raise ValueError("Non-finite decoded audio")
                    result.append(samples)
                    self.frames += len(samples)
        return result

    def close(self):
        if self.native:
            self.native.close()


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
        if native and rate == 48000:
            try:
                from .pcm import NativeMixer
                self.native = NativeMixer()
            except OSError:
                log.warning("Native mixer unavailable; using reference mixer")
        self.inputs: dict[str, MixInput] = {}
        self.gains = {}
        self.lock = threading.Lock()
        self.master = 1.0
        self.call_active = False
        self.call_source = None
        self.underruns = 0

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

    def render(self, frames):
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
    def switch_device(self, device):
        with self._lock:
            if self.closed or self.closing:
                raise RuntimeError("音频连接已结束")
            if device == self.device:
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
    """Use an endpoint's native rate while keeping the Bluetooth mixer at 48 kHz."""
    def __init__(self, mixer, rate):
        self.mixer, self.rate = mixer, rate
        self.chunks = deque()
        self.pending_frames = 0
        self.resampler = None if rate == mixer.rate else av.AudioResampler(format="flt", layout="stereo", rate=rate)

    def render(self, count):
        if self.resampler is None:
            return self.mixer.render(count)
        while self.pending_frames < count:
            pcm = self.mixer.render(480)
            frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), "flt", "stereo")
            frame.sample_rate = self.mixer.rate
            for converted in self.resampler.resample(frame):
                samples = converted.to_ndarray().reshape(-1, 2).copy()
                self.chunks.append(samples)
                self.pending_frames += len(samples)
        out = np.empty((count, 2), dtype=np.float32)
        offset = 0
        while offset < count:
            samples = self.chunks.popleft()
            take = min(len(samples), count - offset)
            out[offset:offset + take] = samples[:take]
            if take < len(samples):
                self.chunks.appendleft(samples[take:])
            self.pending_frames -= take
            offset += take
        return out


class AudioOutput(SwitchableStream):
    @on_audio_thread
    def __init__(self, mixer, output_device=None):
        import sounddevice as sd
        self.mixer = mixer
        self.underflows = 0

        def create(device):
            resolved = resolve_endpoint(device, "output")
            rate = mixer.rate
            try:
                sd.check_output_settings(device=resolved, samplerate=rate, channels=2, dtype="float32")
            except sd.PortAudioError:
                rate = int(sd.query_devices(resolved, "output")["default_samplerate"])
            renderer = OutputRenderer(mixer, rate)

            def callback(outdata, frames, time_info, status):
                if status:
                    self.underflows += 1
                if renderer.resampler is None:
                    mixer.render_into(outdata)
                else:
                    outdata[:] = renderer.render(frames)

            return sd.OutputStream(
                samplerate=rate, channels=2, dtype="float32",
                blocksize=0, latency="low", device=resolved,
                callback=callback,
            )
        super().__init__(create, output_device, "output")

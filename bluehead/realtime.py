"""Native PortAudio callbacks. Python controls lifetime and drains capture off callback.

sounddevice 0.5.6 exposes _StreamBase for library authors providing a C callback.
The DLL, mixer, userdata and callback pointer remain owned until Pa_CloseStream
succeeds. A failed stop/close never frees a callback's native state.
"""
import ctypes as C
import logging
from pathlib import Path
import threading

import numpy as np

log = logging.getLogger(__name__)
_owners = set()


def library():
    api = C.CDLL(str(Path(__file__).parent / "assets/bluehead_audio.dll"))
    pointer, uint = C.c_void_p, C.c_uint32
    api.bh_render_create.argtypes = [pointer, uint]
    api.bh_render_create.restype = pointer
    for name in ("bh_render_halt", "bh_render_resume", "bh_render_free", "bh_capture_reset", "bh_capture_free"):
        getattr(api, name).argtypes = [pointer]
        getattr(api, name).restype = None
    for name in ("bh_render_value", "bh_capture_value"):
        getattr(api, name).argtypes = [pointer, C.c_int]
        getattr(api, name).restype = C.c_uint64
    for name in ("bh_render_process", "bh_capture_push", "bh_capture_read"):
        getattr(api, name).argtypes = [pointer, pointer, uint]
        getattr(api, name).restype = C.c_int
    api.bh_capture_create.argtypes = [uint, uint, uint]
    api.bh_capture_create.restype = pointer
    api.bh_capture_halt.argtypes = [pointer, C.c_int]
    api.bh_capture_halt.restype = None
    for name in ("bh_output_callback", "bh_input_callback"):
        getattr(api, name).argtypes = [pointer, pointer, C.c_ulong, pointer, C.c_ulong, pointer]
        getattr(api, name).restype = C.c_int
    return api


class NativeRender:
    def __init__(self, mixer, rate):
        self.mixer = mixer  # Strong ownership through stream shutdown.
        self.api = library()
        self.handle = self.api.bh_render_create(mixer.handle, int(rate))
        if not self.handle:
            raise ValueError("无法初始化原生音频转换（支持 8–384 kHz）")

    def snapshot(self):
        return {name: int(self.api.bh_render_value(self.handle, index)) for index, name in enumerate(
            ("callbacks", "output_frames", "status_events", "conversion_errors", "max_callback_us", "filter_latency_frames"))}

    def render_into(self, output):
        if (not isinstance(output, np.ndarray) or output.dtype != np.float32 or output.ndim != 2 or
                output.shape[1] != 2 or not output.flags.c_contiguous or not output.flags.writeable or
                not self.handle or not 0 < len(output) <= 48064):
            raise ValueError("Output must be a writable contiguous stereo float32 buffer")
        if self.api.bh_render_process(self.handle, output.ctypes.data, len(output)) < 0:
            raise ValueError("Invalid native render block")

    def close(self):
        if getattr(self, "handle", None):
            self.api.bh_render_free(self.handle)
            self.handle = None

    def __del__(self):
        self.close()


class NativeCapture:
    """SPSC bounded float ring; read() performs SpeexDSP conversion on a worker."""
    def __init__(self, rate, channels, output_rate):
        self.api = library()
        self.channels = channels
        self.handle = self.api.bh_capture_create(int(rate), int(channels), int(output_rate))
        if not self.handle:
            raise ValueError("无法初始化原生麦克风转换")

    def snapshot(self):
        return {name: int(self.api.bh_capture_value(self.handle, index)) for index, name in enumerate(
            ("input_frames", "status_events", "dropped_frames", "queued_frames"))}

    def push(self, samples):
        if (not isinstance(samples, np.ndarray) or samples.dtype != np.float32 or samples.ndim not in (1, 2) or
                (samples.ndim == 1 and self.channels != 1) or
                (samples.ndim == 2 and samples.shape[1] != self.channels) or
                not samples.flags.c_contiguous or not self.handle or len(samples) > 48064):
            raise ValueError("Capture requires contiguous float32 frames with the negotiated channels")
        return self.api.bh_capture_push(self.handle, samples.ctypes.data, len(samples))

    def read_into(self, output):
        if (not isinstance(output, np.ndarray) or output.dtype != np.int16 or output.ndim != 1 or
                not output.flags.c_contiguous or not output.flags.writeable or
                not self.handle or not 0 < len(output) <= 48064):
            raise ValueError("Capture output must be a writable contiguous mono int16 buffer")
        count = self.api.bh_capture_read(self.handle, output.ctypes.data, len(output))
        if count < 0:
            raise RuntimeError("原生麦克风采样率转换失败")
        return count

    def close(self):
        if getattr(self, "handle", None):
            self.api.bh_capture_free(self.handle)
            self.handle = None

    def __del__(self):
        self.close()


class NativeStream:
    """Use sounddevice's library-author API without creating a Python callback."""
    def __init__(self, state, kind, rate, channels, device, extra_settings=None):
        import sounddevice as sd
        self.state, self.kind, self.sd = state, kind, sd
        self.closed = False
        self._close_error = None
        self._stats_lock = threading.Lock()
        self._last = {}
        callback = state.api.bh_output_callback if kind == "output" else state.api.bh_input_callback
        address = C.cast(callback, C.c_void_p).value
        try:
            self.raw = sd._StreamBase(kind=kind, samplerate=rate, channels=channels, device=device,
                                      dtype="float32", blocksize=0, latency="low", extra_settings=extra_settings,
                                      callback=sd._ffi.cast("PaStreamCallback*", address),
                                      userdata=sd._ffi.cast("void*", state.handle), wrap_callback=None)
        except BaseException:
            state.close()  # An unstarted failed open cannot have a live callback.
            raise
        _owners.add(self)

    @property
    def device(self):
        return self.raw.device

    @property
    def active(self):
        return not self.closed and self.raw.active

    def snapshot(self):
        # Telemetry may arrive from the event loop during control-thread close.
        # This lock is never touched by the native realtime callback.
        with self._stats_lock:
            return self._last if self.closed else self.state.snapshot()

    def _release(self):
        with self._stats_lock:
            if self.closed:
                return
            self._last = self.state.snapshot()
            self.state.close()
            self.closed = True
        _owners.discard(self)

    def _halt(self, halted):
        if self.kind == "output":
            (self.state.api.bh_render_halt if halted else self.state.api.bh_render_resume)(self.state.handle)
        else:
            self.state.api.bh_capture_halt(self.state.handle, int(halted))

    def start(self):
        if self.closed:
            raise RuntimeError("音频设备已关闭")
        if self._close_error:
            raise OSError("声音设备未能完全关闭，请退出 BlueHead 后重新打开") from self._close_error
        self._halt(False)
        try:
            self.raw.start()
        except BaseException:
            self._halt(True)
            raise

    def stop(self):
        if self.closed:
            return
        self.raw.stop(ignore_errors=False)
        self._halt(True)

    def _close_raw(self):
        # Pa_CloseStream may invalidate its handle even on failure. Never put
        # that pointer back or retry it. Retain userdata/DLLs until process exit
        # and block PortAudio reinitialization via audio_runtime._retired.
        if self._close_error:
            raise OSError("声音设备未能完全关闭，请退出 BlueHead 后重新打开") from self._close_error
        try:
            self.raw.close(ignore_errors=False)
        except self.sd.PortAudioError as error:
            self._close_error = error
            raise

    def close(self):
        if self.closed:
            return
        self._halt(True)
        self._close_raw()
        self._release()


class PlaybackStream(NativeStream):
    def __init__(self, mixer, rate, device, extra_settings=None):
        if not mixer.native:
            raise OSError("原生音频组件未就绪，请修复或重新解压 BlueHead")
        super().__init__(NativeRender(mixer.native, rate), "output", rate, 2, device, extra_settings)


class MicrophoneStream(NativeStream):
    def __init__(self, rate, channels, output_rate, device, write, emit, extra_settings=None):
        super().__init__(NativeCapture(rate, channels, output_rate), "input", rate, channels, device, extra_settings)
        self.write, self.emit = write, emit
        self._lock, self._quit = threading.Lock(), threading.Event()
        self._running = False
        self._error = None
        self._thread = threading.Thread(target=self._drain, daemon=True, name="microphone-pcm")
        try:
            self._thread.start()
        except BaseException:
            super().close()
            raise

    @property
    def active(self):
        return not self._error and super().active

    def _drain(self):
        output = np.empty(1024, np.int16)
        try:
            while not self._quit.wait(.002):
                with self._lock:
                    if not self._running:
                        continue
                    # A 60 ms queue makes this loop bounded even after a stall.
                    for _ in range(32):
                        count = self.state.read_into(output)
                        if not count:
                            break
                        self.write(output[:count].tobytes())
        except Exception as error:
            self._error = error
            self._halt(True)
            log.exception("Microphone worker stopped")
            self.emit("warning", message=f"麦克风处理已停止，将重新打开设备：{error}")

    def start(self):
        with self._lock:
            if self._error or self._quit.is_set():
                raise RuntimeError("麦克风处理线程已停止")
            if self._running:
                return
            self.state.api.bh_capture_reset(self.state.handle)
            super().start()
            self._running = True

    def stop(self):
        super().stop()
        with self._lock:
            self._running = False
            self.state.api.bh_capture_reset(self.state.handle)

    def close(self):
        if self.closed:
            return
        self._halt(True)
        self._close_raw()  # Callback is gone before the worker or state is freed.
        self._quit.set()
        self._thread.join(2)
        if self._thread.is_alive():
            raise OSError("麦克风处理线程仍在退出，将稍后重试")
        self._release()

"""Native fixed-quality PCM conversion and development array facades.

Production endpoint callbacks live entirely in bluehead_device.dll. NativeCapture
accepts owned bytes for process-excluding loopback; NumPy is imported only by
the independent offline array methods and is absent from release packages.
"""
import ctypes as C
from pathlib import Path




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
        import numpy as np
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

    def push_float_bytes(self, data):
        if (not isinstance(data, bytes) or len(data) % (4 * self.channels) or
                len(data) > 48064 * 4 * self.channels or not self.handle):
            raise ValueError("Invalid float capture PCM")
        return self.api.bh_capture_push(self.handle, data, len(data) // (4 * self.channels))

    def read_pcm(self, maximum=1024):
        if not self.handle or not 0 < maximum <= 48064:
            raise ValueError("Invalid capture PCM capacity")
        output = (C.c_int16 * maximum)()
        count = self.api.bh_capture_read(self.handle, output, maximum)
        if count < 0:
            raise OSError("原生麦克风转换失败")
        return C.string_at(output, count * 2)

    def push(self, samples):
        import numpy as np
        if (not isinstance(samples, np.ndarray) or samples.dtype != np.float32 or samples.ndim not in (1, 2) or
                (samples.ndim == 1 and self.channels != 1) or
                (samples.ndim == 2 and samples.shape[1] != self.channels) or
                not samples.flags.c_contiguous or not self.handle or len(samples) > 48064):
            raise ValueError("Capture requires contiguous float32 frames with the negotiated channels")
        return self.api.bh_capture_push(self.handle, samples.ctypes.data, len(samples))

    def read_into(self, output):
        import numpy as np
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

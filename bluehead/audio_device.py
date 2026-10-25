"""WASAPI control/lifetime and S16 byte draining, with entirely native callbacks.

No NumPy, sounddevice or PyAV. One persistent control thread owns COM and device
lifecycle. Capture read/statistics and close share a lock so a native state is
never freed while the worker still uses it. OS operations report their actual
outcome before rollback; no timeout can leave an unobserved commit in progress.
"""
import ctypes as C
from concurrent.futures import Future
from pathlib import Path
import queue
import threading

ABI_VERSION, BUFFER_SMALL, MAX_FRAMES = 1, -10001, 48064
PLAYBACK, CAPTURE = 1, 2


class Endpoint(C.Structure):
    _fields_ = [("size", C.c_uint32), ("direction", C.c_uint32), ("is_default", C.c_uint32),
                ("reserved", C.c_uint32), ("endpoint_id", C.c_wchar * 64), ("name", C.c_char * 256)]


class DeviceStats(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in (
        "size", "abi", "direction", "sample_rate", "channels", "output_rate", "conversion", "active"
    )] + [(name, C.c_uint64) for name in (
        "callbacks", "frames", "errors", "max_callback_us", "notifications", "dropped_frames",
        "queued_frames", "filter_latency_frames"
    )]


def library():
    api = C.CDLL(str((Path(__file__).parent / "assets/bluehead_device.dll").resolve()))
    ptr, uint = C.c_void_p, C.c_uint32
    api.bh_device_abi.argtypes, api.bh_device_abi.restype = [], uint
    if api.bh_device_abi() != ABI_VERSION:
        raise RuntimeError("原生音频设备模块版本不匹配，请完整更新 BlueHead")
    signatures = {
        "context_create": ([C.c_wchar_p, C.POINTER(C.c_int)], ptr), "context_close": ([ptr], C.c_int),
        "inventory": ([ptr, C.POINTER(Endpoint), uint, C.POINTER(uint)], C.c_int),
        "open_output": ([ptr, ptr, C.c_wchar_p, C.POINTER(C.c_int)], ptr),
        "open_capture": ([ptr, C.c_wchar_p, uint, C.POINTER(C.c_int)], ptr),
        "stats": ([ptr, C.POINTER(DeviceStats)], C.c_int), "read": ([ptr, ptr, uint], C.c_int),
        "offline": ([ptr, uint, ptr, uint, uint, uint, C.POINTER(C.c_int)], ptr),
        "offline_process": ([ptr, ptr, ptr, uint], C.c_int),
    }
    signatures.update({name: ([ptr], C.c_int) for name in ("start", "stop", "close", "active")})
    for name, (arguments, result) in signatures.items():
        function = getattr(api, "bh_device_" + name)
        function.argtypes, function.restype = arguments, result
    return api


def _check(result, operation):
    if result < 0:
        raise OSError(f"{operation}失败（WASAPI {result}）")
    return result


def _endpoint(value):
    if value is None:
        return None
    if not isinstance(value, str) or not 0 < len(value) < 64 or "\0" in value:
        raise ValueError("音频设备标识无效，请重新选择")
    return value


def _mixer(value):
    native = getattr(value, "native", value)
    handle = getattr(native, "handle", native)
    if isinstance(handle, C.c_void_p):
        handle = handle.value
    if not isinstance(handle, int) or handle <= 0:
        raise ValueError("播放需要有效的原生混音器")
    return handle


class Context:
    def __init__(self):
        self.api, self._lock, self._streams = library(), threading.RLock(), set()
        self._close_lock = threading.Lock()
        self.closed = self._closing = False
        self._thread = None
        self._tasks = queue.Queue()
        self._worker = threading.Thread(target=self._run, daemon=True, name="wasapi-device-control")
        self._worker.start()
        error = C.c_int()
        core = str((Path(__file__).parent / "assets/bluehead_audio.dll").resolve())
        try:
            self.handle = self._call(self.api.bh_device_context_create, core, C.byref(error))
            if not self.handle:
                _check(error.value if error.value < 0 else -1, "初始化原生音频设备")
        except BaseException:
            self.closed = True
            self._tasks.put(None)
            self._worker.join()
            raise

    def _run(self):
        self._thread = threading.get_ident()
        while True:
            request = self._tasks.get()
            if request is None:
                return
            future, function, arguments = request
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(function(*arguments))
            except BaseException as error:
                future.set_exception(error)

    def _call(self, function, *arguments, closing=False):
        with self._lock:
            if self.closed or (self._closing and not closing):
                raise RuntimeError("音频设备上下文已关闭")
            if self._thread == threading.get_ident():
                return function(*arguments)
            future = Future()
            self._tasks.put((future, function, arguments))
            return future.result()

    def inventory(self):
        def snapshot():
            capacity = 32
            for _ in range(4):
                if capacity > 4096:
                    raise RuntimeError("音频设备数量超出安全范围")
                records, count = (Endpoint * capacity)(), C.c_uint32()
                result = self.api.bh_device_inventory(self.handle, records, capacity, C.byref(count))
                if result == BUFFER_SMALL:
                    capacity = max(capacity * 2, count.value)
                    continue
                _check(result, "读取音频设备")
                output = [{"endpoint_id": item.endpoint_id, "name": item.name.decode("utf-8", "replace"),
                           "direction": "output" if item.direction == PLAYBACK else "input",
                           "is_default": bool(item.is_default), "max_output_channels": 2 if item.direction == PLAYBACK else 0,
                           "max_input_channels": 1 if item.direction == CAPTURE else 0,
                           "default_samplerate": 48000.0, "hostapi": "WASAPI"}
                          for item in records[:count.value]]
                output.sort(key=lambda item: (item["direction"] != "output", item["endpoint_id"]))
                for index, item in enumerate(output):
                    item["index"] = index
                return output
            raise RuntimeError("音频设备正在变化，请稍后重试")
        return self._call(snapshot)

    def _selection(self, device, endpoint_id):
        if endpoint_id is not None:
            return _endpoint(endpoint_id)
        if device is None or isinstance(device, str):
            return _endpoint(device)
        if not isinstance(device, int) or isinstance(device, bool):
            raise ValueError("音频设备选择无效")
        for item in self.inventory():
            if item["index"] == device:
                return item["endpoint_id"]
        raise ValueError("所选音频设备已断开，请重新选择")

    def _created(self, handle, error, direction, selected, mixer=None):
        if not handle:
            _check(error.value if error.value < 0 else -1, "打开音频设备")
        try:
            stream = Stream(self, handle, direction, selected, mixer)
        except BaseException:
            self._call(self.api.bh_device_close, handle)
            raise
        self._streams.add(stream)
        return stream

    def create_output(self, mixer, device=None, *, endpoint_id=None):
        selected, error = self._selection(device, endpoint_id), C.c_int()
        with self._lock:
            handle = self._call(self.api.bh_device_open_output, self.handle, _mixer(mixer), selected, C.byref(error))
            return self._created(handle, error, PLAYBACK, selected, mixer)

    def create_capture(self, device=None, *, endpoint_id=None, output_rate=48000):
        selected, error = self._selection(device, endpoint_id), C.c_int()
        if not isinstance(output_rate, int) or not 8000 <= output_rate <= 384000:
            raise ValueError("麦克风输出采样率无效")
        with self._lock:
            handle = self._call(self.api.bh_device_open_capture, self.handle, selected, output_rate, C.byref(error))
            return self._created(handle, error, CAPTURE, selected)

    def _offline(self, direction, *, mixer=None, rate=48000, channels=1, output_rate=48000):
        error = C.c_int()
        with self._lock:
            handle = self._call(self.api.bh_device_offline, self.handle, direction,
                                _mixer(mixer) if direction == PLAYBACK else None, rate, channels, output_rate, C.byref(error))
            return self._created(handle, error, direction, None, mixer)

    createOutput, createCapture = create_output, create_capture

    def close(self):
        with self._close_lock:
            self._close()

    def _close(self):
        with self._lock:
            if self.closed:
                return
            self._closing = True
            streams = list(self._streams)
        # No context->stream lock while another close waits in the other order.
        try:
            for stream in streams:
                stream.close()
            with self._lock:
                _check(self._call(self.api.bh_device_context_close, self.handle, closing=True), "关闭音频设备上下文")
                self.handle, self.closed = None, True
        except BaseException:
            with self._lock:
                self._closing = False
            raise
        self._tasks.put(None)
        self._worker.join()


class Stream:
    def __init__(self, owner, handle, direction, endpoint_id, mixer=None):
        self.owner, self.handle, self.direction = owner, handle, direction
        self.endpoint_id, self.mixer = endpoint_id, mixer
        self.closed, self._lock, self._last = False, threading.RLock(), {}
        self._buffer = (C.c_int16 * MAX_FRAMES)() if direction == CAPTURE else None

    def start(self):
        with self._lock:
            if self.closed:
                raise RuntimeError("音频流已关闭")
            _check(self.owner._call(self.owner.api.bh_device_start, self.handle), "启动音频设备")
        return self

    def stop(self):
        with self._lock:
            if not self.closed:
                _check(self.owner._call(self.owner.api.bh_device_stop, self.handle), "停止音频设备")
        return self

    @property
    def active(self):
        with self._lock:
            return not self.closed and bool(self.owner.api.bh_device_active(self.handle))

    def snapshot(self):
        with self._lock:
            if self.closed:
                return dict(self._last)
            value = DeviceStats(size=C.sizeof(DeviceStats))
            _check(self.owner.api.bh_device_stats(self.handle, C.byref(value)), "读取音频设备计数")
            data = {name: int(getattr(value, name)) for name, _ in DeviceStats._fields_}
            data.update({"conversion_mode": "Windows shared SRC" if value.conversion == 0 else "SpeexDSP fallback",
                         "conversion_errors": data["errors"], "status_events": data["notifications"],
                         "output_frames" if self.direction == PLAYBACK else "input_frames": data["frames"]})
            self._last = data
            return dict(data)

    stats = snapshot

    def read(self, frames=1024):
        if not isinstance(frames, int) or not 0 < frames <= MAX_FRAMES:
            raise ValueError("麦克风读取长度无效")
        with self._lock:
            if self.closed or self.direction != CAPTURE:
                raise RuntimeError("麦克风流不可读")
            count = self.owner.api.bh_device_read(self.handle, self._buffer, frames)
            _check(count, "读取麦克风")
            return C.string_at(self._buffer, count * 2)

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.snapshot()
            _check(self.owner._call(self.owner.api.bh_device_close, self.handle, closing=True), "关闭音频设备")
            self.handle, self.closed, self.mixer = None, True, None
            self._last["active"] = 0
            with self.owner._lock:
                self.owner._streams.discard(self)


_default_context = None
_default_lock = threading.Lock()


def context():
    global _default_context
    with _default_lock:
        if _default_context is None or _default_context.closed:
            _default_context = Context()
        return _default_context


def inventory():
    return context().inventory()


def create_output(mixer, device=None, *, endpoint_id=None):
    return context().create_output(mixer, device, endpoint_id=endpoint_id)


def create_capture(device=None, *, endpoint_id=None, output_rate=48000):
    return context().create_capture(device, endpoint_id=endpoint_id, output_rate=output_rate)


createOutput, createCapture = create_output, create_capture

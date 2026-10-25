"""One COM-initialized control thread owns PortAudio. Callbacks never enter it."""
from concurrent.futures import Future
from functools import wraps
import queue
import threading

from .windows_audio import audio_apartment

_instances = set()
_retired = {}
_tasks = queue.Queue()
_thread = None
_start_lock = threading.Lock()
_current = threading.local()
CONTROL_TIMEOUT = 20


class Request:
    def __init__(self):
        self.future = Future()
        self.lock = threading.Lock()
        self.cancelled = self.committed = False


def commit_change():
    request = getattr(_current, "request", None)
    if request is not None:
        with request.lock:
            if request.cancelled:
                raise TimeoutError("音频设备打开超时，已取消此次切换")
            request.committed = True


def _run():
    with audio_apartment():
        while True:
            request, function, args, kwargs = _tasks.get()
            _current.request = request
            request.future.set_running_or_notify_cancel()
            try:
                if request.cancelled:
                    raise TimeoutError("音频操作已取消")
                request.future.set_result(function(*args, **kwargs))
            except BaseException as error:
                request.future.set_exception(error)
            finally:
                _current.request = None


def on_audio_thread(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        global _thread
        if threading.current_thread() is _thread:
            return function(*args, **kwargs)
        with _start_lock:
            if _thread is None:
                _thread = threading.Thread(target=_run, daemon=True, name="wasapi-control")
                _thread.start()
        request = Request()
        _tasks.put((request, function, args, kwargs))
        try:
            return request.future.result(timeout=CONTROL_TIMEOUT)
        except TimeoutError:
            if request.future.done():
                return request.future.result()
            with request.lock:
                if not request.committed:
                    request.cancelled = True
                    raise
            # A destructive OS call already began; wait for its real outcome.
            # Never report rollback while an operation can still commit later.
            return request.future.result()
    return invoke


def register(stream):
    _instances.add(stream)


def unregister(stream):
    _instances.discard(stream)


def close_raw(stream):
    try:
        stream.close()
    except Exception:
        # Keep the Python callback alive if the native handle is not released.
        _retired[id(stream)] = stream
        raise
    else:
        _retired.pop(id(stream), None)


def retry_retired():
    if _retired:
        commit_change()
    for stream in list(_retired.values()):
        try:
            close_raw(stream)
        except Exception:
            pass


@on_audio_thread
def refresh_devices(selections):
    """Close every owned stream before rebuilding PortAudio's static device list."""
    import sounddevice as sd
    from .endpoints import portaudio_id
    retry_retired()
    if _retired:
        raise RuntimeError("音频设备仍在释放中，将稍后重试")
    live = [s for s in _instances if not s.closed]
    desired = {direction: portaudio_id(index) if index is not None else None
               for direction, index in selections.items()}
    records = [(s, s.device, portaudio_id(s.stream.device) if s.device is not None else None, s.running)
               for s in live]
    # If a close fails, do not invalidate the runtime underneath a live callback.
    closed = []
    commit_change()
    try:
        for record in records:
            close_raw(record[0].stream)
            closed.append(record)
    except Exception:
        # Roll back streams already closed; never terminate under a live one.
        for stream, selection, _, was_running in closed:
            stream.stream = stream._factory(selection)
            if was_running:
                stream.stream.start()
        raise
    sd._terminate()
    sd._initialize()
    apis = sd.query_hostapis()
    endpoints = {}
    for i, info in enumerate(sd.query_devices()):
        if "WASAPI" in apis[info["hostapi"]]["name"]:
            identity = portaudio_id(i)
            if identity:
                endpoints[identity] = i
    mapped = {direction: endpoints.get(identity) if identity else None for direction, identity in desired.items()}
    errors = []
    for stream, old_selection, identity, was_running in records:
        selected = endpoints.get(identity) if old_selection is not None else None
        candidate = None
        try:
            candidate = stream._factory(selected)
            stream.stream = candidate
            if was_running:
                stream.stream.start()
            stream.device = selected
        except Exception as error:
            errors.append(str(error))
            if candidate is not None:
                try:
                    close_raw(candidate)
                except Exception as cleanup:
                    errors.append(str(cleanup))
            fallback_stream = None
            try:
                # An explicitly selected endpoint may have disappeared mid-scan.
                fallback_stream = stream._factory(None)
                stream.stream = fallback_stream
                if was_running:
                    stream.stream.start()
                stream.device = None
            except Exception as fallback:
                errors.append(str(fallback))
                if fallback_stream is not None:
                    try:
                        close_raw(fallback_stream)
                    except Exception as cleanup:
                        errors.append(str(cleanup))
                # Keep the object registered for a later device-change retry.
                stream.device = None
        if getattr(stream, "direction", None) in mapped:
            mapped[stream.direction] = stream.device
    return mapped, errors


@on_audio_thread
def streams_unhealthy():
    retry_retired()
    if _retired:
        return True
    for stream in list(_instances):
        if stream.closed or not stream.running:
            continue
        try:
            if not stream.stream.active:
                return True
        except Exception:
            return True
    return False

"""One COM-initialized thread serializes audio route transactions. Callbacks never enter it."""
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
def refresh_devices(selections, identities=None):
    """Reopen changed native endpoints without invalidating any backend context.

    A successful candidate is opened before the old consumer is stopped. A
    failed candidate leaves the previous stream available for rollback.
    """
    from .endpoints import portaudio_id, audio_inventory
    retry_retired()
    desired = {direction: (identities or {}).get(direction) or portaudio_id(index)
               for direction, index in selections.items()}
    live = [s for s in _instances if not s.closed]
    records = [(s, s.device, getattr(s.stream, "endpoint_id", None) or portaudio_id(s.device))
               for s in live]
    endpoints = {item["endpoint_id"]: item["index"] for item in audio_inventory()}
    mapped = {direction: endpoints.get(identity) if identity else None for direction, identity in desired.items()}
    errors = []
    commit_change()
    for stream, old_selection, identity in records:
        selected = endpoints.get(identity) if old_selection is not None else None
        try:
            stream.switch_device(selected, force=True)
        except Exception as error:
            errors.append(str(error))
            # Replaced devices may have vanished. Default capture/render is a
            # separate transaction; failed starts still cannot leak callbacks.
            try:
                stream.switch_device(None, force=True)
            except Exception as fallback:
                errors.append(str(fallback))
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

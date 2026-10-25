"""Behavior at a real endpoint-list generation boundary, using shuffled fake IDs."""

from types import SimpleNamespace
import weakref

from bluehead.audio import SwitchableStream
import bluehead.audio_runtime as runtime


def test_refresh_maps_stable_ids_and_reopens_explicit_and_default_native_routes(monkeypatch):
    phase = [0]
    opened = []
    old_ids, new_ids = {1: "headset", 2: "mic"}, {8: "mic", 9: "headset"}
    monkeypatch.setattr(runtime, "_instances", weakref.WeakSet())
    monkeypatch.setattr(
        "bluehead.endpoints.portaudio_id", lambda index: (old_ids if phase[0] == 0 else new_ids).get(index)
    )
    monkeypatch.setattr(
        "bluehead.endpoints.audio_inventory",
        lambda: [{"endpoint_id": identity, "index": index} for index, identity in new_ids.items()],
    )

    class Raw:
        def __init__(self, selected):
            self.device = selected
            self.endpoint_id = (old_ids if phase[0] == 0 else new_ids).get(selected)
            self.active = self.closed = False
            opened.append(self)

        def start(self):
            assert not self.closed
            self.active = True

        def stop(self):
            self.active = False

        def close(self):
            self.stop()
            self.closed = True

    explicit = SwitchableStream(Raw, 1, "output")
    automatic = SwitchableStream(Raw, None, "input")
    explicit.start()
    automatic.start()
    phase[0] = 1
    try:
        mapped, errors = runtime.refresh_devices({"output": 1, "input": None}, {"output": "headset"})
        assert not errors and mapped == {"output": 9, "input": None}
        assert explicit.device == 9 and explicit.stream.endpoint_id == "headset" and explicit.stream.active
        assert automatic.device is None and automatic.stream.active
        assert all(stream.closed for stream in opened[:2])
    finally:
        explicit.close()
        automatic.close()


def test_slow_endpoint_open_cannot_commit_after_timeout(monkeypatch):
    import threading
    import pytest
    from .test_hot_switch import FakeStream

    active = set()
    release = threading.Event()
    opened = {}

    def factory(device):
        if device == 2:
            assert release.wait(2)
        opened[device] = FakeStream(active, device)
        return opened[device]

    stream = SwitchableStream(factory, 1)
    stream.start()
    monkeypatch.setattr(runtime, "CONTROL_TIMEOUT", 0.02, raising=False)
    timer = threading.Timer(0.1, release.set)
    timer.start()
    try:
        with pytest.raises(TimeoutError):
            stream.switch_device(2)
        timer.join()
        monkeypatch.setattr(runtime, "CONTROL_TIMEOUT", 2)
        runtime.on_audio_thread(lambda: None)()  # Drain the late completion.
        assert active == {1} and stream.device == 1
        assert opened[2].closed
    finally:
        release.set()
        timer.join()
        monkeypatch.setattr(runtime, "CONTROL_TIMEOUT", 2)
        stream.close()


def test_failed_refreshed_endpoint_is_closed_before_default_fallback(monkeypatch):
    phase = [0]
    opened = []
    monkeypatch.setattr(runtime, "_instances", weakref.WeakSet())
    monkeypatch.setattr("bluehead.endpoints.portaudio_id", lambda index: "headset" if index in (1, 9) else None)
    monkeypatch.setattr("bluehead.endpoints.audio_inventory", lambda: [{"endpoint_id": "headset", "index": 9}])

    class Raw:
        def __init__(self, selected):
            self.device = selected
            self.endpoint_id = "headset" if selected is not None else None
            self.fail = phase[0] and selected is not None
            self.closed = self.active = False
            opened.append(self)

        def start(self):
            if self.fail:
                raise RuntimeError("endpoint start failed")
            self.active = True

        def stop(self):
            self.active = False

        def close(self):
            self.stop()
            self.closed = True

    stream = SwitchableStream(Raw, 1, "output")
    stream.start()
    phase[0] = 1
    try:
        mapped, errors = runtime.refresh_devices({"output": 1}, {"output": "headset"})
        assert errors and mapped["output"] is None and stream.stream.active
        assert opened[1].closed, "Failed replacement leaked its native stream"
        assert opened[0].closed and stream.device is None
        assert sum(item.active for item in opened) == 1
    finally:
        stream.close()


def test_failed_close_can_be_retried_and_does_not_lie_about_native_lifetime():
    attempts = []

    class Raw:
        def close(self):
            attempts.append(True)
            if len(attempts) == 1:
                raise RuntimeError("busy closing")

    import pytest

    stream = SwitchableStream(lambda device: Raw(), None)
    with pytest.raises(RuntimeError, match="busy"):
        stream.close()
    assert not stream.closed
    stream.close()
    assert stream.closed and len(attempts) == 2


def test_inactive_stream_is_detected_without_an_endpoint_identity_change(monkeypatch):
    stream = SimpleNamespace(closed=False, running=True, stream=SimpleNamespace(active=False))
    monkeypatch.setattr(runtime, "_instances", [stream])
    assert runtime.streams_unhealthy()
    stream.stream.active = True
    assert not runtime.streams_unhealthy()


def test_completion_at_timeout_boundary_returns_the_real_result(monkeypatch):
    from concurrent.futures import Future

    class DeadlineRace(Future):
        def result(self, timeout=None):
            if timeout is not None:
                # Completion wins just after the waiter reaches its deadline.
                super().result(timeout=2)
                raise TimeoutError()
            return super().result()

    monkeypatch.setattr(runtime, "Future", DeadlineRace)
    assert runtime.on_audio_thread(lambda: "committed")() == "committed"

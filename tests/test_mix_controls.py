from dataclasses import asdict
import asyncio
from io import StringIO
import json
import threading

import numpy as np
import pytest

from bluehead.backend import Receiver, Settings
from bluehead.audio import Mixer
from bluehead.calls import CallManager, CallRouter, CaptureRoute
from bluehead.engine import Engine, validate_settings
from .test_protocols import Network


def command(action, **values):
    return {"protocol": 1, "command": action, **values}


def test_all_levels_persist_and_downlink_applies_to_both_media_and_voice(tmp_path):
    engine = Engine(tmp_path, StringIO())
    receiver = Receiver(engine.settings, tmp_path, lambda *args, **kwargs: None)
    engine.runner.receiver = receiver
    engine.command(command("gain", key="phone", direction="down", gain=1.8))
    engine.command(command("gain", key="phone", direction="up", gain=.3))
    engine.command(command("master_gain", bus="playback", gain=2))
    engine.command(command("master_gain", bus="microphone", gain=.2))
    engine.command(command("master_gain", bus="system", gain=1.4))
    receiver.mixer.add("phone")
    receiver.mixer.add("call:phone")
    assert receiver.mixer.inputs["phone"].gain == receiver.mixer.inputs["call:phone"].gain == 1.8
    assert receiver.mixer.master == 2
    loaded = Engine(tmp_path, StringIO())
    assert loaded.settings.device_volumes == {"phone": {"down": 1.8, "up": .3}}
    assert (loaded.settings.playback_gain, loaded.settings.microphone_gain, loaded.settings.system_gain) == (2, .2, 1.4)
    assert json.loads((tmp_path / "settings.json").read_text("utf-8"))["system_audio_enabled"] is False


@pytest.mark.parametrize("value", [-.01, 2.01, True, float("nan"), float("inf")])
def test_invalid_gain_never_changes_audio_or_saved_levels(tmp_path, value):
    engine = Engine(tmp_path, StringIO())
    with pytest.raises(ValueError):
        engine.command(command("master_gain", bus="playback", gain=value))
    with pytest.raises(ValueError):
        engine.command(command("gain", key="phone", direction="up", gain=value))
    settings = asdict(Settings(transport="usb:8087:0033#0", device_volumes={"phone": {"up": value}}))
    with pytest.raises(ValueError):
        validate_settings(settings)
    assert engine.settings.playback_gain == 1 and not engine.settings.device_volumes


def test_uplink_limiter_preserves_relative_levels_and_closed_system_route_cannot_inject_audio():
    router = CallRouter()
    router.select("phone")
    router.microphone_gain = 2
    route = CaptureRoute(router)
    route.activate()
    route.write_system("phone", np.full(120, 1200, "<i2").tobytes())
    router.clear_system()
    route.close()
    route.write_system("phone", np.full(120, 1200, "<i2").tobytes())
    assert not any(router.read("phone", 240))
    router.write_microphone("phone", np.array([30000, 15000], "<i2").tobytes())
    result = np.frombuffer(router.read("phone", 4), "<i2")
    assert result[0] == 32767 and abs(int(result[0]) - int(result[1]) * 2) <= 1


async def test_system_audio_toggle_keeps_voice_link_and_clears_old_system_samples(monkeypatch):
    manager = CallManager(Network(1).devices[0], Mixer(), lambda *a, **k: None)
    link = object()
    manager.links["phone"] = link
    manager.statistics["phone"] = {"rate": 16000}
    manager.router.select("phone")
    opened = []
    class Capture:
        def __init__(self, route, key, rate, emit):
            self.route, self.closed = route, False
            opened.append(self)
        def close(self):
            self.closed = True
    monkeypatch.setattr('bluehead.loopback.SystemCapture', Capture)
    try:
        await manager.set_system_audio(True)
        assert manager.links["phone"] is link and manager.system_capture is opened[0]
        manager.system_route.write_system("phone", b'1234')
        assert manager.router.system_buffer
        await manager.set_system_audio(False)
        await manager.wait_closed()
        assert opened[0].closed and not manager.router.system_buffer
        assert manager.links["phone"] is link and manager.system_capture is None
    finally:
        manager.close()
        await manager.wait_closed()


async def test_late_system_capture_cannot_inject_into_a_reconnected_call(monkeypatch):
    opened, release, closed = threading.Event(), threading.Event(), threading.Event()
    class SlowCapture:
        def __init__(self, route, key, rate, emit):
            opened.set()
            assert release.wait(3)
            route.write_system(key, b"old computer audio")
        def close(self):
            closed.set()
    monkeypatch.setattr('bluehead.loopback.SystemCapture', SlowCapture)
    manager = CallManager(Network(1).devices[0], Mixer(), lambda *a, **k: None)
    link = object()
    manager.system_enabled = True
    manager.links['phone'] = link
    manager.router.select('phone')
    task = manager.system_capture_tasks['phone'] = manager.spawn(manager.open_system_capture('phone', link, 16000))
    try:
        async with asyncio.timeout(2):
            while not opened.is_set():
                await asyncio.sleep(.005)
        manager.audio_closed('phone', link)
        await asyncio.sleep(0)
        manager.router.select('phone')
        release.set()
        async with asyncio.timeout(2):
            while not closed.is_set():
                await asyncio.sleep(.005)
        assert task.cancelled() and manager.system_capture is None
        assert manager.router.read('phone', 20) == bytes(20)
    finally:
        release.set()
        manager.close()
        await manager.wait_closed()


async def test_cancelled_system_mode_request_still_acknowledges_the_ui():
    from types import SimpleNamespace
    from bluehead.backend import Runner
    events = []
    class Calls:
        async def set_system_audio(self, enabled):
            raise asyncio.CancelledError()
    runner = Runner(lambda kind, **data: events.append((kind, data)))
    runner.receiver = SimpleNamespace(calls=Calls(), settings=Settings())
    runner.loop = asyncio.get_running_loop()
    runner.set_system_audio(True, 99)
    async with asyncio.timeout(1):
        while not events:
            await asyncio.sleep(.005)
    assert events == [("system_audio", {"success": False, "enabled": False, "request_id": 99,
                                      "message": "通话或接收已停止，电脑声音混合未启用"})]

"""Endpoint stalls and late OS completions must not stall or revive a call."""
import asyncio
import threading
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from bumble import hci, hfp

from bluehead.audio import Mixer
from bluehead.calls import CallManager, Msbc
from .test_protocols import Network


def test_application_can_exit_while_os_microphone_open_is_stuck():
    child = """
import asyncio
import threading
from types import SimpleNamespace
from bumble import hci, hfp
from bumble.device import Device
from bumble.host import Host
from bluehead.audio import Mixer
import bluehead.calls as calls
opening = threading.Event()
class StuckMicrophone:
    def __init__(self, *args):
        opening.set()
        threading.Event().wait(30)
    def close(self):
        pass
calls.Microphone = StuckMicrophone
async def run():
    device = Device(host=Host())
    device.host.send_sco_sdu = lambda *args: None
    manager = calls.CallManager(device, Mixer(), lambda *args, **kwargs: None)
    link = SimpleNamespace(handle=71, air_mode=hci.CodecID.TRANSPARENT, once=lambda *args: None)
    manager.on_link('phone', SimpleNamespace(active_codec=hfp.AudioCodec.MSBC), link)
    while not opening.is_set():
        await asyncio.sleep(.005)
    manager.close()
    await manager.wait_closed()
asyncio.run(run())
print('clean exit')
"""
    result = subprocess.run([sys.executable, "-c", child], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and "clean exit" in result.stdout, result.stderr


def voice_link():
    listeners = {}
    link = SimpleNamespace(handle=71, air_mode=hci.CodecID.TRANSPARENT,
                           once=lambda name, callback: listeners.update({name: callback}))
    link.listeners = listeners
    return link


def packet():
    data = Msbc().encode(np.full(120, 6000, dtype="<i2").tobytes())
    return hci.HCI_SynchronousDataPacket(connection_handle=71, packet_status=0,
                                        data_total_length=len(data), data=data)


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.005)


@pytest.mark.parametrize("end", ["hangup", "shutdown", "replace"])
async def test_slow_microphone_leaves_hci_running_and_late_result_is_closed(monkeypatch, end):
    import bluehead.calls as calls
    opening, release, closed = threading.Event(), threading.Event(), threading.Event()
    instances = []

    class SlowMicrophone:
        frames = 0
        def __init__(self, router, key, rate, device, emit):
            instances.append(self)
            self.closed = False
            opening.set()
            assert release.wait(3)
            # A late callback from an obsolete capture must not reach a new call.
            router.write_microphone(key, b"obsolete microphone")
        def close(self):
            self.closed = True
            closed.set()

    monkeypatch.setattr(calls, "Microphone", SlowMicrophone)
    mixer = Mixer(buffer_ms=0)
    manager = CallManager(Network(1).devices[0], mixer, lambda *args, **kwargs: None)
    uplink = []
    manager.device.host.send_sco_sdu = lambda handle, data: uplink.append(data)
    protocol = SimpleNamespace(active_codec=hfp.AudioCodec.MSBC)
    first = voice_link()
    try:
        manager.on_link("phone", protocol, first)
        await until(opening.is_set)
        # This runs while OS microphone opening is still blocked in another thread.
        first.sink(packet())
        assert len(uplink) == 2
        assert manager.statistics["phone"]["decoded_frames"] == 120
        assert mixer.inputs["call:phone"].frames > 0
        assert manager.microphone is None
        if end == "shutdown":
            manager.close()
        else:
            manager.audio_closed("phone", first)
            if end == "replace":
                replacement = voice_link()
                manager.on_link("phone", protocol, replacement)
                first.listeners["disconnection"](0)
                assert manager.links["phone"] is replacement
        # Let cancellation mark the old request abandoned before its OS call returns.
        await asyncio.sleep(0)
        release.set()
        await until(closed.is_set)
        if end == "replace":
            await until(lambda: manager.microphone is not None)
            assert not manager.microphone.closed
            assert manager.router.read("phone", 19) == bytes(19)
        else:
            assert manager.microphone is None and manager.router.owner is None
        assert instances[0].closed
    finally:
        release.set()
        manager.close()
        await manager.wait_closed()


async def test_missing_microphone_keeps_phone_audio_decoding(monkeypatch):
    import bluehead.calls as calls
    events = []
    def unavailable(*args):
        raise RuntimeError("endpoint unavailable")
    monkeypatch.setattr(calls, "Microphone", unavailable)
    mixer = Mixer(buffer_ms=0)
    manager = CallManager(Network(1).devices[0], mixer, lambda kind, **data: events.append((kind, data)))
    manager.device.host.send_sco_sdu = lambda *args: None
    link = voice_link()
    try:
        manager.on_link("phone", SimpleNamespace(active_codec=hfp.AudioCodec.MSBC), link)
        await manager.capture_tasks["phone"]
        link.sink(packet())
        assert manager.statistics["phone"]["decoded_frames"] == 120
        assert mixer.inputs["call:phone"].frames > 0
        assert manager.microphone is None
        assert any(kind == "error" and "endpoint unavailable" in data["message"] for kind, data in events)
    finally:
        manager.close()
        await manager.wait_closed()


async def test_hot_selection_waits_for_pending_open_then_uses_requested_endpoint(monkeypatch):
    import bluehead.calls as calls
    opening, release = threading.Event(), threading.Event()
    selected = []
    class Microphone:
        def __init__(self, router, key, rate, device, emit):
            opening.set()
            assert release.wait(3)
            selected.append(device)
        def switch_device(self, device):
            selected.append(device)
        def close(self):
            pass
    monkeypatch.setattr(calls, "Microphone", Microphone)
    manager = CallManager(Network(1).devices[0], Mixer(), lambda *args, **kwargs: None)
    manager.device.host.send_sco_sdu = lambda *args: None
    link = voice_link()
    try:
        manager.on_link("phone", SimpleNamespace(active_codec=hfp.AudioCodec.MSBC), link)
        await until(opening.is_set)
        change = asyncio.create_task(manager.set_input_device(9))
        await asyncio.sleep(0)
        assert not change.done()
        release.set()
        await asyncio.wait_for(change, 2)
        assert selected == [None, 9] and manager.input_device == 9
        assert manager.links["phone"] is link
    finally:
        release.set()
        manager.close()
        await manager.wait_closed()


async def test_timed_out_capture_is_disposed_and_hot_selection_recovers(monkeypatch):
    import bluehead.calls as calls
    opening, release, closed = threading.Event(), threading.Event(), threading.Event()
    instances = []
    class Microphone:
        def __init__(self, router, key, rate, device, emit):
            self.device, self.closed = device, False
            instances.append(self)
            if device is None:
                opening.set()
                assert release.wait(3)
                router.write_microphone(key, b"stale")
        def close(self):
            self.closed = True
            closed.set()
    monkeypatch.setattr(calls, "Microphone", Microphone)
    monkeypatch.setattr(calls, "MICROPHONE_OPEN_TIMEOUT", .05)
    manager = CallManager(Network(1).devices[0], Mixer(), lambda *args, **kwargs: None)
    manager.device.host.send_sco_sdu = lambda *args: None
    link = voice_link()
    try:
        manager.on_link("phone", SimpleNamespace(active_codec=hfp.AudioCodec.MSBC), link)
        await until(opening.is_set)
        await manager.capture_tasks["phone"]
        assert manager.microphone is None and not instances[0].closed
        await manager.set_input_device(9)
        current = manager.microphone
        assert current.device == 9 and manager.links["phone"] is link
        release.set()
        await until(closed.is_set)
        assert instances[0].closed and not current.closed
        assert manager.router.read("phone", 5) == bytes(5)
    finally:
        release.set()
        manager.close()
        await manager.wait_closed()

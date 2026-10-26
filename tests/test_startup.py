"""Control setup must finish, fail or honor Stop without retaining HCI waits."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bluehead import backend, discovery
from bluehead.native_control.transport import NativeTransport


@pytest.mark.parametrize('event', ['timeout', 'stop', 'lost', 'cancel', 'success', 'failure'])
async def test_initialization_has_bounded_wait_and_preserves_transport_lifetime(tmp_path, monkeypatch, event):
    receiver = backend.Receiver(backend.Settings(calls=False), tmp_path, lambda *a, **kw: None)
    entered, retired = asyncio.Event(), asyncio.Event()
    terminated = asyncio.get_running_loop().create_future()

    async def power_on():
        entered.set()
        try:
            if event == 'success':
                return
            if event == 'failure':
                raise OSError('controller command rejected')
            await asyncio.Future()
        finally:
            retired.set()

    inquiry = AsyncMock()
    monkeypatch.setattr(discovery, 'update_inquiry_power', inquiry)
    receiver.advertise = AsyncMock()
    transport = SimpleNamespace(source=SimpleNamespace(terminated=terminated))
    task = asyncio.create_task(receiver.initialize_transport(
        SimpleNamespace(power_on=power_on), SimpleNamespace(name='offline'), transport, timeout=.05))
    await entered.wait()
    if event == 'stop':
        receiver.stop_event.set()
        assert await task is False
    elif event == 'lost':
        terminated.set_result(None)
        with pytest.raises(ConnectionError, match='启动时断开'):
            await task
    elif event == 'cancel':
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif event == 'timeout':
        with pytest.raises(TimeoutError, match='启动超时'):
            await task
    elif event == 'failure':
        with pytest.raises(OSError, match='rejected'):
            await task
    else:
        assert await task is True
    assert retired.is_set()
    assert not terminated.cancelled()  # The transport owns this shared Future.
    assert inquiry.await_count == receiver.advertise.await_count == (event == 'success')
    # No pending initializer/stop waiter survives to operate on a later session.
    assert task.done()


@pytest.mark.parametrize('event', ['success', 'stop', 'cancel', 'firmware-error'])
async def test_native_firmware_wait_is_cancellable_and_host_never_runs_in_bootloader(tmp_path, monkeypatch, event):
    stage = {'ready': False, 'closed': False}

    class Client:
        def request(self, op, **values):
            if op == 'usb_close':
                stage['closed'] = True
            if op != 'usb_poll':
                return True
            return dict(packets=[], starting=not stage['ready'], active=event != 'firmware-error',
                        firmware_state='downloading',
                        error='Intel firmware: bundled image missing' if stage['ready'] and event == 'firmware-error' else '')

    called = asyncio.Event()

    async def power_on():
        assert stage['ready']
        called.set()

    receiver = backend.Receiver(backend.Settings(calls=False), tmp_path, lambda *a, **kw: None)
    monkeypatch.setattr(discovery, 'update_inquiry_power', AsyncMock())
    receiver.advertise = AsyncMock()
    async with NativeTransport(Client(), 'usb:8087:0033#0') as transport:
        task = asyncio.create_task(receiver.initialize_transport(
            SimpleNamespace(power_on=power_on), SimpleNamespace(name='offline'), transport, timeout=.1))
        await asyncio.sleep(.02)
        assert not called.is_set()
        if event == 'stop':
            receiver.stop_event.set()
            assert await task is False
        elif event == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            stage['ready'] = True
            if event == 'firmware-error':
                with pytest.raises(OSError, match='启动固件缺失或损坏'):
                    await task
            else:
                assert await task is True
        assert called.is_set() == (event == 'success')
    assert stage['closed'] and transport.source.task.done()

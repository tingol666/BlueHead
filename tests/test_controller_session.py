"""Session capability degradation must preserve saved preferences and quality."""
import asyncio
from types import SimpleNamespace

from bluehead.backend import Receiver, Settings
from bluehead.native_control.transport import ControllerStartupError


def test_media_only_does_not_advertise_voice_or_change_saved_quality():
    async def check():
        original = Settings(calls=True, codec='ldac', buffer_ms=60)
        receiver = object.__new__(Receiver)
        receiver.settings = original
        receiver.stop_event = asyncio.Event()
        events = []
        receiver.emit = lambda event, **values: events.append((event, values))

        async def ready(_):
            return None

        transport = SimpleNamespace(wait_ready=ready, source=SimpleNamespace(startup={
            'capabilities': {'media_transport': True, 'voice_transport': False}}))
        assert await receiver.prepare_native_transport(transport)
        assert not receiver.settings.calls and original.calls
        assert receiver.settings.codec == 'ldac' and receiver.settings.buffer_ms == original.buffer_ms
        assert {event for event, _ in events} == {'controller_capabilities', 'warning'}
    asyncio.run(check())


def test_stop_cancels_firmware_wait_before_publishing_services():
    async def check():
        receiver = object.__new__(Receiver)
        receiver.stop_event = asyncio.Event()
        receiver.emit = lambda *_args, **_kw: None
        cancelled = asyncio.Event()

        async def blocked(_):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        task = asyncio.create_task(receiver.prepare_native_transport(SimpleNamespace(wait_ready=blocked)))
        await asyncio.sleep(0)
        receiver.stop_event.set()
        assert not await asyncio.wait_for(task, .5)
        assert cancelled.is_set()
    asyncio.run(check())


def test_firmware_failure_preserves_machine_readable_remedy():
    error = ControllerStartupError({'error': 'Firmware: verified artifact unavailable',
                                    'firmware_error': 'missing_firmware', 'firmware_profile': 'realtek-8761bu'})
    assert error.category == 'missing_firmware' and error.firmware_profile == 'realtek-8761bu'
    assert '原厂驱动引导' in str(error)

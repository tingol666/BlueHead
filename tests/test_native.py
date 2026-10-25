import asyncio

import pytest

from bluehead.native import NativeReceiver


@pytest.mark.asyncio
async def test_native_com_lifecycle_on_background_thread():
    # Stop before enumeration/opening: verify COM setup without changing any audio route.
    def worker():
        async def run():
            stop = asyncio.Event()
            stop.set()
            await NativeReceiver(lambda *args, **kwargs: None).run(stop)
        asyncio.run(run())
    await asyncio.to_thread(worker)

"""Cancellation-safe wrapper around Bumble's one HCI command owner.

Caller cancellation retires caller intent, not an already transmitted command.
The native USB owner measures the response deadline, so Python blocking cannot
cause a false controller timeout. No packet sender/receiver is introduced here.
"""

import asyncio
import types


class CommandOwner:
    def __init__(self, host, failed, *, native_deadline=True):
        self.host, self.failed, self.native_deadline = host, failed, native_deadline
        self.original = host._send_command
        self.lock, self.tasks = asyncio.Lock(), set()
        self.closed = False

        async def guarded(owner, command, response_timeout=None):
            return await self.send(command, response_timeout)

        host._send_command = types.MethodType(guarded, host)

    async def send(self, command, timeout):
        if self.closed:
            raise ConnectionError("The HCI command owner has retired")

        async def transaction():
            async with self.lock:
                if self.closed:
                    raise ConnectionError("The HCI command owner has retired")
                try:
                    return await self.original(command, None if self.native_deadline else timeout)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # Poison BEFORE releasing our gate. A queued command cannot
                    # reuse a channel whose preceding response is ambiguous.
                    self.closed = True
                    self.failed(error)
                    raise

        task = asyncio.create_task(transaction())
        self.tasks.add(task)

        def done(result):
            self.tasks.discard(result)
            if not result.cancelled():
                result.exception()

        task.add_done_callback(done)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # If nothing was transmitted this queued intent can be cancelled.
            # Otherwise retain both native deadline and the existing Host future
            # until that exact response or whole-transport loss retires it.
            if self.host.pending_command is not command:
                task.cancel()
            raise

    def close(self):
        self.closed = True
        for task in tuple(self.tasks):
            task.cancel()

    async def wait_closed(self):
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)

"""Bumble signalling adapter. Physical USB remains in the native runtime."""
import asyncio
import contextlib
import re

class _Source:
    def __init__(self, client):
        self.client, self.sink = client, None
        self.failure = None
        self.failure_category = ''
        self.terminated = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self.poll())

    def set_packet_sink(self, sink):
        self.sink = sink

    async def poll(self):
        try:
            while True:
                batch = self.client.request("usb_poll")
                for packet in batch["packets"]:
                    if self.sink:
                        self.sink.on_packet(bytes.fromhex(packet))
                if not batch["active"]:
                    reason = batch.get('reason', '')
                    if reason and reason != 'device_removed':
                        self.failure_category = reason
                        self.failure = ConnectionError(
                            '蓝牙控制队列积压，接收已停止，请重新开始接收' if reason == 'control_backpressure' else
                            '蓝牙传输已停止，请重新开始接收')
                    break
                await asyncio.sleep(.004 if not batch["packets"] else 0)
        except asyncio.CancelledError:
            return
        except (OSError, ValueError) as error:
            self.failure = error
            self.failure_category = 'native_channel'
        finally:
            if not self.terminated.done():
                self.terminated.set_result(None)


class _Sink:
    def __init__(self, client):
        self.client = client

    def on_packet(self, packet):
        self.client.request("usb_send", packet=bytes(packet).hex())


class NativeTransport:
    def __init__(self, client, spec):
        match = re.fullmatch(r"usb:([0-9a-fA-F]{4}):([0-9a-fA-F]{4})#(\d{1,2})(?:\+sco=0)?", spec)
        if not match:
            raise ValueError("原生接收需要有效的 USB 适配器")
        self.client = client
        self.client.request("usb_open", vid=int(match[1], 16), pid=int(match[2], 16), index=int(match[3]))
        self.source, self.sink = _Source(client), _Sink(client)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, _error, _trace):
        self.source.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.source.task
        try:
            self.client.request("usb_close")
        except OSError:
            if exc_type is None:
                raise

"""Bounded AVDTP transactions on Bumble's existing signalling channel.

Timed-out labels are quarantined until this channel closes: a late reply must
never complete a newer codec/stream command. Audio continues in the native owner.
"""

import asyncio
from bumble import avdtp


class Transactions:
    def __init__(self, protocol, *, timeout=2.0):
        self.protocol, self.timeout = protocol, timeout
        self.lock = asyncio.Lock()
        self.pending, self.retired = {}, set()
        self.next_label, self.closed = 0, False
        self.incoming = protocol.message_assembler.callback
        protocol.message_assembler.callback = self.receive
        protocol.send_command = self.send
        protocol.on('close', self.close)

    async def send(self, command):
        async with asyncio.timeout(self.timeout + 1):
            async with self.lock:
                if self.closed:
                    raise ConnectionError('这台设备的媒体连接已断开')
                label = next((index % 16 for index in range(self.next_label, self.next_label + 16)
                              if index % 16 not in self.retired), None)
                if label is None:
                    raise ConnectionError('设备多次未响应，请重新连接这台设备后再调整编码')
                self.next_label = (label + 1) % 16
                future = asyncio.get_running_loop().create_future()
                self.pending[label] = (command.signal_identifier, future)
                try:
                    self.protocol.send_message(label, command)
                    response = await asyncio.wait_for(asyncio.shield(future), self.timeout)
                    if response.message_type in (avdtp.Message.MessageType.GENERAL_REJECT,
                                                  avdtp.Message.MessageType.RESPONSE_REJECT):
                        raise avdtp.ProtocolError(getattr(response, 'error_code', 0), 'avdtp')
                    return response
                except (TimeoutError, asyncio.CancelledError):
                    self.retired.add(label)
                    raise
                finally:
                    self.pending.pop(label, None)
                    if not future.done():
                        future.cancel()
                    elif not future.cancelled():
                        future.exception()

    def receive(self, label, message):
        if message.message_type == avdtp.Message.MessageType.COMMAND:
            if not self.closed:
                self.incoming(label, message)
            return
        item = self.pending.get(label)
        if item is not None and item[0] == message.signal_identifier and not item[1].done():
            item[1].set_result(message)

    def close(self):
        self.closed = True
        for _, future in tuple(self.pending.values()):
            if not future.done():
                future.set_exception(ConnectionError('这台设备的媒体连接已断开'))

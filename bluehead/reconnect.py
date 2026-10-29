"""Optional, bounded link-loss recovery on the receiver's existing HCI owner.

The peer's own connection policy is independent. Disabled means no local
solicitation, not rejection of incoming connections. User/peer disconnects,
unknown bonds, voice isolation and retired receiver generations are respected.
No codec negotiation, playback command or PCM processing is changed here.
"""

import asyncio
import contextlib
import logging
from bumble import avdtp
from bumble.core import PhysicalTransport

log = logging.getLogger(__name__)
LINK_LOSS = (0x08, 0x22)


class LinkReconnect:
    def __init__(self, receiver, *, delays=(1.0, 3.0, 8.0)):
        self.receiver = receiver
        self.enabled = bool(getattr(receiver.settings, 'auto_reconnect', False))
        self.delays = delays
        self.listeners, self.tasks = {}, {}
        self.closed = False

    def watch(self, owner):
        if self.closed or not self.enabled or id(owner) in self.listeners:
            return

        def lost(reason):
            self.listeners.pop(id(owner), None)
            # Profile and receiver listeners must retire the old ACL first.
            asyncio.get_running_loop().call_soon(self.lost, owner, reason)

        self.listeners[id(owner)] = (owner, lost)
        owner.once('disconnection', lost)

    def available(self, key):
        receiver = self.receiver
        guard = receiver.voice_protection
        return (self.enabled and not self.closed and not receiver.retiring
                and not receiver.stop_event.is_set() and receiver.device is not None
                and key not in receiver.connection_keys()
                and len(receiver.connection_keys()) < receiver.settings.maximum_devices
                and not (guard and guard.enabled and guard.reservation))

    def lost(self, owner, reason):
        key = str(owner.peer_address)
        if reason not in LINK_LOSS or not self.available(key):
            return
        if owner.transport == PhysicalTransport.LE:
            app = self.receiver.le_audio
            if (app is not None and getattr(owner, '_bluehead_bonded', False)
                    and getattr(owner, 'encryption', False)):
                app.request_reconnect(owner)
            return
        if key not in self.tasks:
            task = self.tasks[key] = asyncio.create_task(self.classic(key, owner.peer_address))

            def done(result):
                if self.tasks.get(key) is result:
                    self.tasks.pop(key, None)
                if not result.cancelled() and result.exception() is not None:
                    log.warning('Classic reconnect ended: %s', type(result.exception()).__name__)

            task.add_done_callback(done)

    async def classic(self, key, address):
        receiver, device = self.receiver, self.receiver.device
        store = getattr(device, 'keystore', None)
        keys = await store.get(key) if store else None
        if not getattr(keys, 'link_key', None):
            return
        for attempt, delay in enumerate(self.delays, 1):
            await asyncio.sleep(delay)
            if not self.available(key):
                return
            receiver.emit('reconnect', key=key, phase='connecting', attempt=attempt,
                          message='连接意外中断，正在尝试重新连接。')
            try:
                owner = await device.connect(address, transport=PhysicalTransport.BR_EDR, timeout=5)
            except Exception as error:
                log.info('Classic reconnect attempt %d ended: %s', attempt, type(error).__name__)
                continue
            # Existing connection handlers own HFP/AVRCP and stream lifetimes.
            # Give the sender first use of its usual A2DP setup, then open only
            # the signalling channel if it has not done so. Never auto-play.
            await asyncio.sleep(1)
            if self.closed or receiver.retiring or receiver.connections.get(key) is not owner:
                return
            try:
                async with asyncio.timeout(4):
                    await self.media_control(owner)
            except Exception as error:
                log.info('Reconnected ACL retained; media control not ready: %s', type(error).__name__)
            if receiver.connections.get(key) is owner and not self.closed and not receiver.retiring:
                receiver.emit('reconnect', key=key, phase='connected', message='蓝牙已重新连接。')
            return
        if self.available(key):
            receiver.emit('reconnect', key=key, phase='waiting',
                          message='暂未连上，请在发送设备的蓝牙设置中点击 BlueHead。')

    async def media_control(self, owner):
        receiver = self.receiver
        listener = getattr(receiver, 'listener', None)
        if listener is None or owner.handle in listener.servers:
            return
        protocol = await avdtp.Protocol.connect(owner)
        if (self.closed or receiver.retiring or receiver.connections.get(str(owner.peer_address)) is not owner
                or owner.handle in listener.servers):
            with contextlib.suppress(Exception):
                await protocol.l2cap_channel.disconnect()
            return
        listener.set_server(owner, protocol)

        def closed():
            # A late close cannot remove a newer protocol on a reused handle.
            if listener.servers.get(owner.handle) is protocol:
                listener.remove_server(owner)

        protocol.on('close', closed)
        receiver.on_protocol(protocol)

    async def close(self):
        self.closed = True
        for owner, lost in self.listeners.values():
            owner.remove_listener('disconnection', lost)
        self.listeners.clear()
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

"""Bounded CAP content-control discovery on the peer's existing encrypted ACL.

No radio, stream, PCM, pairing key or separate HCI ownership is created here.
Media/telephone services are optional on the peer; their absence is recorded,
not confused with failure of the already established native audio stream.
"""

import asyncio
import contextlib
from bumble import gatt
from bumble.device import Peer


class PeerAudioControl:
    def __init__(self, app):
        self.app, self.tasks, self.subscriptions = app, {}, {}
        self.closed = False

    def start(self, owner):
        if (self.closed or not self.app.admitted(owner) or not getattr(owner, 'encryption', False)
                or not hasattr(owner, 'gatt_client') or id(owner) in self.tasks):
            return
        task = asyncio.create_task(self.discover(owner))
        self.tasks[id(owner)] = task
        # Retain the completed entry: a service read must not trigger rediscovery.
        task.add_done_callback(lambda result: None if result.cancelled() else result.exception())

    async def discover(self, owner):
        found, subscriptions = [], []
        try:
            async with asyncio.timeout(6):
                peer = Peer(owner)
                for uuid in (gatt.GATT_GENERIC_MEDIA_CONTROL_SERVICE, gatt.GATT_MEDIA_CONTROL_SERVICE,
                             gatt.GATT_GENERIC_TELEPHONE_BEARER_SERVICE, gatt.GATT_TELEPHONE_BEARER_SERVICE):
                    for service in (await peer.discover_service(uuid))[:4]:
                        if not self.app.admitted(owner):
                            return
                        await service.discover_characteristics()
                        found.append(str(uuid))
                        for characteristic in service.characteristics[:64]:
                            if characteristic.uuid == gatt.GATT_CONTENT_CONTROL_ID_CHARACTERISTIC:
                                await characteristic.read_value()
                            if characteristic.uuid in (gatt.GATT_MEDIA_STATE_CHARACTERISTIC,
                                                       gatt.GATT_CALL_STATE_CHARACTERISTIC):
                                def changed(data, uuid=characteristic.uuid):
                                    if self.app.admitted(owner):
                                        self.app.emit('le_audio_control', key=str(owner.peer_address),
                                                      characteristic=str(uuid), length=len(data))
                                if characteristic.properties & gatt.Characteristic.Properties.NOTIFY:
                                    await characteristic.subscribe(changed)
                                    subscriptions.append((characteristic, changed))
                                if characteristic.properties & gatt.Characteristic.Properties.READ:
                                    await characteristic.read_value()
                if self.app.admitted(owner):
                    self.subscriptions[id(owner)] = subscriptions
                    self.app.emit('le_audio_discovery', key=str(owner.peer_address),
                                  phase='services', content_services=len(found))
        except Exception as error:
            # Malformed/absent peer services cannot tear down the native route.
            if self.app.admitted(owner):
                self.app.emit('le_audio_discovery', key=str(owner.peer_address), phase='services_partial',
                              reason='对端控制服务未完整响应，音频连接将继续尝试。',
                              error=type(error).__name__)

    def disconnected(self, owner):
        task = self.tasks.pop(id(owner), None)
        if task and not task.done():
            task.cancel()
        self.subscriptions.pop(id(owner), None)

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.subscriptions.clear()

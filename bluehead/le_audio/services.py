"""Encrypted, connection-owned ASCS adapter for Bumble's existing GATT host.

Only signalling crosses Python. The injected bridge owns native stream leases;
this service never receives ISO/PCM, opens USB or creates a packet sender.
"""

import asyncio
import contextlib
import logging
from bumble import att, gatt
from bumble.device import CisLink
from .ascs_control import StreamControl
from .endpoints import State
from ..errors import SourceExpired

log = logging.getLogger(__name__)


class AudioStreamService(gatt.TemplateService):
    UUID = gatt.GATT_AUDIO_STREAM_CONTROL_SERVICE

    def __init__(
        self,
        device,
        bridge,
        *,
        microphone=True,
        maximum_peers=3,
        emit=lambda *a, **k: None,
        source_octets=400,
        source_blocks=2,
        enable_timeout=8.0,
    ):
        self.device, self.bridge, self.notify_engine = device, bridge, emit
        self.admit = lambda owner: True
        self.peer_activity = lambda owner: None
        self.discovery_activity = lambda owner, phase: None
        self.control = StreamControl(
            maximum_peers, microphone=microphone, source_octets=source_octets, source_blocks=source_blocks
        )
        if hasattr(bridge, "attach"):
            bridge.attach(self.control)
        if hasattr(bridge, "set_controller"):
            bridge.set_controller(device)
        if not 0.01 <= enable_timeout <= 30:
            raise ValueError("Invalid LE setup timeout")
        self.enable_timeout, self.ready_timers = enable_timeout, {}
        self.closed = False
        self.locks, self.pending, self.watched = {}, {}, {}
        self.prepared, self.bound, self.links, self.routes = set(), set(), {}, {}
        self.tasks, self.writers = set(), set()
        self.listeners = []
        self.ases = {}
        for ase in (1, 2, 3) if microphone else (1, 2):
            characteristic = gatt.Characteristic(
                gatt.GATT_SOURCE_ASE_CHARACTERISTIC if ase == 3 else gatt.GATT_SINK_ASE_CHARACTERISTIC,
                gatt.Characteristic.Properties.READ | gatt.Characteristic.Properties.NOTIFY,
                gatt.Characteristic.Permissions.READABLE
                | gatt.Characteristic.Permissions.READ_REQUIRES_ENCRYPTION,
                gatt.CharacteristicValue(read=lambda connection, ase=ase: self.read_ase(connection, ase)),
            )
            self.ases[ase] = characteristic
        self.control_point = gatt.Characteristic(
            gatt.GATT_ASE_CONTROL_POINT_CHARACTERISTIC,
            gatt.Characteristic.Properties.WRITE
            | gatt.Characteristic.Properties.WRITE_WITHOUT_RESPONSE
            | gatt.Characteristic.Properties.NOTIFY,
            gatt.Characteristic.Permissions.WRITEABLE | gatt.Characteristic.Permissions.WRITE_REQUIRES_ENCRYPTION,
            gatt.CharacteristicValue(write=self.write_control),
        )
        super().__init__([*self.ases.values(), self.control_point])
        self._listen(device, device.EVENT_CIS_REQUEST, lambda link: self.spawn(self.accept(link)))

    def _listen(self, emitter, event, callback):
        emitter.on(event, callback)
        self.listeners.append((emitter, event, callback))

    def forget(self, emitter):
        for source, event, callback in tuple(self.listeners):
            if source is emitter:
                with contextlib.suppress(ValueError, KeyError):
                    source.remove_listener(event, callback)
                self.listeners.remove((source, event, callback))

    def spawn(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.tasks.add(task)

        def done(result):
            self.tasks.discard(result)
            if not result.cancelled() and result.exception():
                log.warning("LE control task failed (%s)", type(result.exception()).__name__)

        task.add_done_callback(done)
        return task

    def current(self, owner):
        return (
            not self.closed
            and self.admit(owner)
            and any(connection is owner for connection in self.device.connections.values())
        )

    def watch(self, owner):
        if not self.current(owner) or not getattr(owner, "encryption", False):
            raise att.ATT_Error(att.ATT_INSUFFICIENT_ENCRYPTION_ERROR)
        self.peer_activity(owner)
        self.discovery_activity(owner, "services")
        identity = id(owner)
        if identity not in self.watched:
            try:
                self.control.peer(owner, create=True)
            except ValueError:
                raise att.ATT_Error(att.ATT_INSUFFICIENT_RESOURCES_ERROR) from None
            self.watched[identity] = owner
            self.locks[identity] = asyncio.Lock()
            self._listen(owner, "disconnection", lambda reason: self.disconnected(owner))
        return self.locks[identity]

    def read_ase(self, owner, ase):
        self.watch(owner)
        return self.control.value(owner, ase)

    async def notify(self, owner, characteristic, value):
        if self.current(owner) and getattr(owner, "encryption", False):
            await self.device.notify_subscriber(owner, characteristic, value)

    async def state_changed(self, token):
        if self.control.current(token):
            await self.notify(token.owner, self.ases[token.ase], self.control.value(token.owner, token.ase))

    async def write_control(self, owner, data):
        lock = self.watch(owner)
        identity = id(owner)
        if self.pending.get(identity, 0) >= 8:
            raise att.ATT_Error(att.ATT_INSUFFICIENT_RESOURCES_ERROR)
        self.pending[identity] = self.pending.get(identity, 0) + 1
        writer = asyncio.current_task()
        self.writers.add(writer)
        try:
            async with lock:
                if not self.current(owner):
                    return
                result = self.control.write(owner, bytes(data), secured=bool(owner.encryption))
                if any(endpoint.codec is not None for endpoint in self.control.peer(owner).pool.endpoints.values()):
                    self.discovery_activity(owner, 'configured')
                await self.notify(owner, self.control_point, result.response)
                for ase, value in result.notifications:
                    await self.notify(owner, self.ases[ase], value)
                for effect in result.effects:
                    if effect.action == "stop":
                        await self.stop(effect.token)
                        if effect.ticket and self.control.finish_release(effect.ticket):
                            await self.notify(
                                owner, self.ases[effect.ticket.ase], self.control.value(owner, effect.ticket.ase)
                            )
                    elif self.control.current(effect.token):
                        endpoint = self.control.peer(owner).pool.endpoints[effect.token.ase]
                        try:
                            await self.bridge.prepare(effect.token, endpoint.codec, endpoint.qos)
                            if self.control.current(effect.token):
                                self.prepared.add(effect.token)
                                self.ready_timers[effect.token] = asyncio.get_running_loop().call_later(
                                    self.enable_timeout, lambda token=effect.token: self.spawn(self.expire(token))
                                )
                            else:
                                await self.stop(effect.token)
                        except Exception as error:
                            await self.fail(effect.token, error)
                if self.current(owner):
                    for link in tuple(self.links.values()):
                        if link.acl_connection is owner:
                            await self.activate(link)
        finally:
            if identity in self.watched:
                self.pending[identity] = max(0, self.pending.get(identity, 1) - 1)
            else:
                self.pending.pop(identity, None)
            self.writers.discard(writer)

    def matching(self, link):
        try:
            peer = self.control.peer(link.acl_connection)
        except ValueError:
            return []
        return [
            endpoint.token
            for endpoint in peer.pool.endpoints.values()
            if endpoint.token
            and peer.pool.cis_matches(endpoint.token, link.acl_connection, link.cig_id, link.cis_id)
        ]

    async def accept(self, link):
        owner = link.acl_connection
        try:
            lock = self.watch(owner)
        except att.ATT_Error:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.device.reject_cis_request(link), 2)
            return
        async with lock:
            tokens = self.matching(link)
            if not tokens or any(token not in self.prepared for token in tokens):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.device.reject_cis_request(link), 2)
                return
            try:
                await asyncio.wait_for(self.device.accept_cis_request(link), 3)
                if not self.current(owner) or not self.matching(link):
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(link.disconnect(), 2)
                    return
                self.links[link.handle] = link
                self._listen(link, "disconnection", lambda reason: self.spawn(self.link_lost(link)))
                await self.activate(link)
            except Exception as error:
                for token in tokens:
                    await self.fail(token, error)

    async def activate(self, link):
        for token in self.matching(link):
            if token in self.bound or token not in self.prepared:
                continue
            endpoint = self.control.peer(token.owner).pool.endpoints[token.ase]
            if token.source and not endpoint.receiver_ready:
                continue
            direction = (
                CisLink.Direction.HOST_TO_CONTROLLER if token.source else CisLink.Direction.CONTROLLER_TO_HOST
            )
            try:
                self.routes[token] = link
                # Receive route must precede controller delivery. Transmit must
                # wait for both the controller path and remote Receiver Ready.
                if token.source:
                    await asyncio.wait_for(link.setup_data_path(direction=direction), 2)
                await self.bridge.bind(token, link)
                if not self.control.current(token):
                    await self.stop(token)
                    continue
                if not token.source:
                    await asyncio.wait_for(link.setup_data_path(direction=direction), 2)
                if self.control.native_ready(token, token.owner, link.cig_id, link.cis_id):
                    self.bound.add(token)
                    timer = self.ready_timers.pop(token, None)
                    if timer:
                        timer.cancel()
                    await self.state_changed(token)
                else:
                    await self.stop(token)
            except Exception as error:
                await self.fail(token, error)

    async def expire(self, token):
        lock = self.locks.get(id(token.owner))
        if lock is not None:
            async with lock:
                if self.control.current(token) and token not in self.bound:
                    await self.fail(token, TimeoutError("Peer did not finish LE audio setup"))

    async def stop(self, token):
        timer = self.ready_timers.pop(token, None)
        if timer:
            timer.cancel()
        self.prepared.discard(token)
        self.bound.discard(token)
        link = self.routes.pop(token, None)
        try:
            await self.bridge.stop(token)
        except Exception as error:
            log.warning("LE stream retirement failed (%s)", type(error).__name__)
        if (
            link is not None
            and self.links.get(link.handle) is link
            and not any(other.source == token.source and route is link for other, route in self.routes.items())
        ):
            direction = (
                CisLink.Direction.HOST_TO_CONTROLLER if token.source else CisLink.Direction.CONTROLLER_TO_HOST
            )
            try:
                await asyncio.wait_for(link.remove_data_path([direction]), 2)
            except Exception as error:
                log.warning("LE data-path retirement failed (%s)", type(error).__name__)

    async def fail(self, token, error):
        await self.stop(token)
        if self.control.current(token):
            peer = self.control.peer(token.owner)
            ticket = peer.pool.release(token)
            await self.notify(token.owner, self.ases[token.ase], bytes([token.ase, State.RELEASING]))
            if peer.pool.finish_release(ticket):
                await self.notify(token.owner, self.ases[token.ase], self.control.value(token.owner, token.ase))
            if isinstance(error, SourceExpired):
                self.notify_engine(
                    "warning",
                    message="这个设备的蓝牙连接已变化，已取消旧的音频请求。请在设备上重新连接 BlueHead。",
                    category="le_audio_connection_changed",
                )
            else:
                self.notify_engine(
                    "error", message="音频通道未能准备，请断开并重新连接这个设备。", category="le_audio_stream"
                )
        log.warning("LE stream setup failed (%s)", type(error).__name__)

    async def link_lost(self, link):
        if self.links.get(link.handle) is not link:
            return
        self.links.pop(link.handle)
        self.forget(link)
        lock = self.locks.get(id(link.acl_connection))
        if lock is None:
            return
        async with lock:
            for token in self.matching(link):
                peer = self.control.peer(token.owner)
                old, fresh = peer.pool.disable(token)
                if token.source:
                    peer.pool.receiver_stopped(fresh)
                await self.stop(old)
                await self.state_changed(fresh)

    def disconnected(self, owner):
        tokens = self.control.disconnected(owner)
        for handle, link in tuple(self.links.items()):
            if link.acl_connection is owner:
                self.links.pop(handle)
                self.forget(link)
        self.forget(owner)
        if tokens:

            async def retire():
                for token in tokens:
                    await self.stop(token)

            self.spawn(retire())
        self.watched.pop(id(owner), None)
        self.locks.pop(id(owner), None)
        self.pending.pop(id(owner), None)

    async def close(self):
        self.closed = True
        for timer in self.ready_timers.values():
            timer.cancel()
        self.ready_timers.clear()
        tokens = []
        for owner in tuple(self.watched.values()):
            tokens.extend(self.control.disconnected(owner))
        for emitter, event, callback in self.listeners:
            with contextlib.suppress(ValueError, KeyError):
                emitter.remove_listener(event, callback)
        self.listeners.clear()
        for token in tokens:
            await self.stop(token)
        current = asyncio.current_task()
        pending = (self.tasks | self.writers) - {current}
        if pending:
            done, pending = await asyncio.wait(pending, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self.links.clear()
        self.routes.clear()
        self.prepared.clear()
        self.bound.clear()
        self.watched.clear()
        self.locks.clear()
        self.pending.clear()

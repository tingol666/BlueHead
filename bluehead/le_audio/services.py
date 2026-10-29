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
from .cis_acceptance import CisAcceptance, CisSuperseded
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
        supported_phys=3,
    ):
        self.device, self.bridge, self.notify_engine = device, bridge, emit
        self.admit = lambda owner: True
        self.peer_activity = lambda owner: None
        self.discovery_activity = lambda owner, phase: None
        self.control = StreamControl(
            maximum_peers, microphone=microphone, source_octets=source_octets, source_blocks=source_blocks,
            supported_phys=supported_phys,
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
        self.accepting = {}
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
        self._listen(device, device.EVENT_CIS_REQUEST, self.incoming)
        self._listen(device, "cis_establishment", self.established)

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
                log.info("LE ASCS control: acl=%s opcode=%s response=%s", owner.handle,
                         data[0] if data else -1, result.response.hex())
                for endpoint in self.control.peer(owner).pool.endpoints.values():
                    if endpoint.codec is not None and endpoint.qos is not None:
                        log.info("LE ASCS configuration: ase=%s rate=%s channels=%s duration_us=%s octets=%s blocks=%s qos=%s",
                                 endpoint.ase, endpoint.codec.rate, endpoint.codec.channels,
                                 endpoint.codec.duration_us, endpoint.codec.octets, endpoint.codec.blocks, endpoint.qos)
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

    def begin_accept(self, link):
        previous = self.accepting.get(link.handle)
        if previous is not None:
            previous.supersede()
        stale = self.links.get(link.handle)
        if stale is not None and stale is not link:
            self.links.pop(link.handle)
            self.forget(stale)
            self.spawn(self.link_lost(stale, retired=True))
        attempt = CisAcceptance(self.device, link)
        self.accepting[link.handle] = attempt
        return attempt

    def incoming(self, link):
        # Event installation must precede the next HCI packet in the batch.
        previous = self.accepting.get(link.handle)
        if previous is not None and previous.same_request(link):
            previous.follow(link)
            log.info("LE duplicate pending CIS coalesced: cis=%s", link.handle)
            return previous.task
        attempt = self.begin_accept(link)
        attempt.task = self.spawn(self.accept(link, attempt))
        return attempt.task

    def established(self, link):
        attempt = self.accepting.get(link.handle)
        if (attempt is None or attempt.link is not link) and self.links.get(link.handle) is not link:
            self.spawn(self.retire_unowned(link))

    async def retire_unowned(self, link):
        attempt = self.accepting.get(link.handle)
        if (self.device.cis_links.get(link.handle) is link
                and link.state == CisLink.State.ESTABLISHED
                and (attempt is None or attempt.link is not link)
                and not any(route is link for route in self.routes.values())):
            try:
                await asyncio.wait_for(link.disconnect(), 2)
            except Exception as error:
                log.warning("LE unowned CIS retirement failed: cis=%s error=%s", link.handle, type(error).__name__)

    async def accept(self, link, attempt=None):
        owner = link.acl_connection
        attempt = attempt or self.begin_accept(link)
        if attempt.task is None:
            attempt.task = asyncio.current_task()
        tokens = []
        log.info("LE CIS request: acl=%s cis=%s cig=%s stream=%s", owner.handle, link.handle, link.cig_id, link.cis_id)
        try:
            lock = self.watch(owner)
            async with lock:
                tokens = self.matching(link)
                ready = bool(tokens) and all(token in self.prepared for token in tokens)
            if not ready:
                with contextlib.suppress(Exception):
                    if self.device.cis_links.get(link.handle) is link and not attempt.result.done():
                        await asyncio.wait_for(self.device.reject_cis_request(link), 2)
                return
            # Neither the peer lock nor Bumble's global CIS convenience lock
            # may be held while awaiting an asynchronous controller event.
            await asyncio.wait_for(attempt.run(), 3)
            link = attempt.link
            async with lock:
                if (self.accepting.get(link.handle) is not attempt or not self.current(owner)
                        or not self.matching(link) or self.device.cis_links.get(link.handle) is not link):
                    return
                log.info("LE CIS established: cis=%s", link.handle)
                self.links[link.handle] = link
                self._listen(link, "disconnection", lambda reason: self.spawn(self.link_lost(link)))
                await self.activate(link)
        except CisSuperseded:
            log.info("LE CIS attempt superseded: cis=%s", link.handle)
        except att.ATT_Error:
            with contextlib.suppress(Exception):
                if self.device.cis_links.get(link.handle) is link and not attempt.result.done():
                    await asyncio.wait_for(self.device.reject_cis_request(link), 2)
        except Exception as error:
            if self.accepting.get(link.handle) is attempt and self.current(owner):
                lock = self.locks.get(id(owner))
                if lock is not None:
                    async with lock:
                        for token in tokens:
                            if self.control.current(token):
                                await self.fail(token, error)
        finally:
            attempt.close()
            if self.accepting.get(link.handle) is attempt:
                self.accepting.pop(link.handle, None)
            await self.retire_unowned(link)

    async def activate(self, link):
        if self.device.cis_links.get(link.handle) is not link:
            return
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
        log.warning("LE stream setup failed: ase=%s source=%s error=%s", token.ase, token.source,
                    type(error).__name__, exc_info=(type(error), error, error.__traceback__))

    async def link_lost(self, link, *, retired=False):
        if not retired and self.links.get(link.handle) is not link:
            return
        if self.links.get(link.handle) is link:
            self.links.pop(link.handle)
        self.forget(link)
        lock = self.locks.get(id(link.acl_connection))
        if lock is None:
            return
        async with lock:
            for token in tuple(token for token, route in self.routes.items() if route is link):
                if not self.control.current(token):
                    await self.stop(token)
                    continue
                peer = self.control.peer(token.owner)
                old, fresh = peer.pool.disable(token)
                if token.source:
                    peer.pool.receiver_stopped(fresh)
                await self.stop(old)
                await self.state_changed(fresh)

    def disconnected(self, owner):
        for attempt in tuple(self.accepting.values()):
            if attempt.link.acl_connection is owner:
                attempt.disconnected(0)
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
        for attempt in self.accepting.values():
            attempt.close()
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

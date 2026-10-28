"""Application composition for LE profiles, native leases and shared media policy.

Bumble owns signalling; NativeStreamBridge owns scoped audio routes; the existing
Receiver owns UI intent and media arbitration. No parallel PCM/USB implementation.
"""

import asyncio
import contextlib
from dataclasses import replace
import struct
import time
from types import SimpleNamespace

from bumble import avdtp, hci, gatt
from bumble.core import PhysicalTransport
from .profile import ControllerSupport, create_services, CommonAudioService
from .native_bridge import NativeStreamBridge
from .broadcast_receiver import BroadcastReceiver, NativeBroadcastSink
from .bass_service import BroadcastScanService
from .audio_controls import AudioControls
from .discovery import AudioAdvertisement, HEADSET_APPEARANCE, announcement, roles
from .peer_control import PeerAudioControl
from .service_cache import ServiceCache
from ..protocol import Format
from ..errors import Retirement


class LeMediaSession:
    """Protocol-neutral facade consumed by the existing media handoff policy."""

    def __init__(self, app, key):
        self.app, self.key = app, key
        self.records = {}
        self.closed, self.paused, self.allowed = False, False, True
        self.started_at, self.paused_at = time.monotonic(), 0
        self.format = Format("lc3", 48000, 1)
        self.stream = self

    @property
    def state(self):
        return avdtp.State.OPEN if self.paused else avdtp.State.STREAMING

    def _states(self):
        states = []
        for record in tuple(self.records.values()):
            if not record.closing and record.prepared:
                with contextlib.suppress(OSError):
                    states.append(self.app.client.request("le_rx_snapshot", **self.app.bridge.parameters(record)))
        return states

    @property
    def last_packet(self):
        states = self._states()
        age = min((state.get("last_packet_age_ms", 1e12) for state in states), default=1e12)
        return time.monotonic() - age / 1000

    def set_allowed(self, allowed):
        self.allowed = bool(allowed)
        for record in tuple(self.records.values()):
            if record.lease and not record.closing:
                with contextlib.suppress(OSError):
                    self.app.client.request(
                        "mix_gate", key=record.key, lease=record.lease, enabled=self.allowed and not self.paused
                    )

    def resume(self):
        if not self.closed:
            self.paused = False
            self.started_at = time.monotonic()
            self.set_allowed(self.allowed)

    def pause(self):
        if not self.closed:
            self.paused = True
            self.paused_at = time.monotonic()
            self.set_allowed(self.allowed)

    async def start(self):
        self.resume()

    async def stop(self):
        self.pause()

    def snapshot(self):
        states = self._states()
        return dict(
            codec="lc3",
            rate=self.format.rate,
            channels=self.format.channels,
            selected=self.allowed,
            bitrate=sum(value.get("bitrate", 0) for value in states),
            frames=sum(value.get("frames", 0) for value in states),
            packets=sum(value.get("packets", 0) for value in states),
            packets_per_second=sum(value.get("packets_per_second", 0) for value in states),
            errors=sum(value.get("errors", 0) for value in states),
            dropped=sum(value.get("dropped", 0) for value in states),
            protocol="LE Audio",
            protected=False,
            playing=not self.paused
            and any(value.get("active") and value.get("last_packet_age_ms", 1000000) < 500 for value in states),
            concealed_blocks=sum(value.get("concealed_blocks", 0) for value in states),
            presentation_ms=max(
                (record.budget.presentation_us / 1000 for record in self.records.values() if record.budget),
                default=0,
            ),
            queue_budget_ms=max(
                (record.budget.queue_us / 1000 for record in self.records.values() if record.budget), default=0
            ),
            buffer_ms=max((value.get("buffer_ms", 0) for value in states), default=0),
        )

    def close(self):
        self.set_allowed(False)
        self.closed = True
        self.records.clear()


class LeAudioApplication:
    def __init__(self, receiver, device, support, *, input_opener=None):
        self.receiver, self.device, self.support = receiver, device, support
        self.client = receiver.mixer.client
        self.emit = receiver.emit
        self.connections, self.media, self.listeners = {}, {}, []
        self.identities = {}
        self.closed = False
        self.advertise_lock = asyncio.Lock()
        self.announcer = AudioAdvertisement(device)
        self.peer_controls = PeerAudioControl(self)
        self.bonded = False
        self.failure = asyncio.get_running_loop().create_future()
        self.bridge = NativeStreamBridge(
            self.client,
            input_endpoint=receiver.settings.input_endpoint_id,
            system_audio=receiver.settings.system_audio_enabled,
            system_endpoint=getattr(receiver.settings, "system_endpoint_id", None),
            gains=receiver.settings.device_volumes,
            protection=receiver.voice_protection,
            input_opener=input_opener,
            emit=self.emit,
            media_changed=self.media_changed,
            source_changed=self.source_changed,
        )
        self.bridge.failure = self.controller_failed

        # Device emits this synchronously while the native packet context is
        # still current; asynchronous ASE work only consumes the captured tag.
        def cis_identity(link):
            link._bluehead_link_epoch = getattr(getattr(device, "host", None), "hci_metadata", {}).get(
                "bluehead_link_epoch", 0
            )

        device.on(device.EVENT_CIS_REQUEST, cis_identity)
        self.listeners.append((device, device.EVENT_CIS_REQUEST, cis_identity))
        from .latency import PresentationBudget

        self.budget = PresentationBudget(self.client)
        self.bridge.latency = self.budget
        self.client.request(
            "router_gain",
            device=1,
            microphone=receiver.settings.microphone_gain,
            system=receiver.settings.system_gain,
            muted=receiver.settings.microphone_muted,
        )
        self.bundle = (
            create_services(
                device,
                self.bridge,
                support,
                microphone=receiver.settings.calls,
                maximum_peers=receiver.settings.maximum_devices,
                emit=self.emit,
            )
            if support.receive
            else None
        )
        self.broadcast = (
            BroadcastReceiver(device, NativeBroadcastSink(self.client), emit=self.emit)
            if support.broadcast
            else None
        )
        self.bass = BroadcastScanService(device, self.broadcast) if self.broadcast else None
        if self.bundle:
            self.bundle.streams.admit = self.admitted
            self.bundle.streams.control.delay_caps = self.budget.capabilities
            self.bundle.streams.peer_activity = self.peer_controls.start
        if self.broadcast:
            self.broadcast.failure = self.controller_failed
        if self.bass:
            self.bass.admit = self.admitted
            self.broadcast.sink.latency = self.budget
        self.previous_voice_prepare = getattr(receiver.voice_protection, "before_prepare", None)
        if receiver.voice_protection:
            receiver.voice_protection.before_prepare = self.prepare_voice
        if self.broadcast:
            self.broadcast.can_play = lambda: (
                not (
                    receiver.voice_protection
                    and receiver.voice_protection.enabled
                    and receiver.voice_protection.reservation
                )
            )
        self.controls = AudioControls(
            device,
            self.set_render_gain,
            self.bridge.set_remote_mute if support.transmit and receiver.settings.calls else None,
            private=lambda: receiver.settings.microphone_muted,
        )
        self.controls.admit = self.admitted
        services = list(self.bundle.services) if self.bundle else [CommonAudioService()]
        services.extend(self.controls.services)
        if self.bass:
            services.append(self.bass)
        host = getattr(device, 'host', None)
        le_2m = bool(host and host.supports_le_features(hci.LeFeatureMask.LE_2M_PHY))
        self.tmap_roles = roles(receive=bool(self.bundle),
                                transmit=support.transmit and receiver.settings.calls,
                                broadcast=bool(self.broadcast), le_2m=le_2m)
        if self.tmap_roles:
            from bumble.profiles.tmap import TelephonyAndMediaAudioService
            services.append(TelephonyAndMediaAudioService(self.tmap_roles))
        device.add_services(services)
        # Match the extended advertisement's headset appearance in GAP itself.
        server = getattr(device, 'gatt_server', None)
        for attribute in getattr(server, 'attributes', ()):
            if attribute.type == gatt.GATT_APPEARANCE_CHARACTERISTIC:
                attribute.value = struct.pack('<H', HEADSET_APPEARANCE)
        self.service_cache = ServiceCache(self)

    async def set_render_gain(self, value):
        await self.bridge.set_render_gain(value)
        if self.broadcast:
            await self.broadcast.sink.render_gain(value)

    @classmethod
    async def prepare(cls, receiver, device, transport):
        # The packet pump is the sole consumer of usb_poll. Snapshot is read-only.
        status = await asyncio.to_thread(receiver.mixer.client.request, "snapshot")
        support = ControllerSupport.parse(status.get("usb", {}))
        if not device.supports_le_features(hci.LeFeatureMask.LE_EXTENDED_ADVERTISING):
            support = replace(support, receive=False, transmit=False, broadcast=False)
            receiver.emit('le_audio_discovery', phase='unavailable',
                          reason='这个适配器当前不能发送 LE Audio 所需的扩展广播，已使用经典蓝牙。')
        if support.receive:
            # Enable the host ISO feature only after actual transport/features
            # are known. Unsupported LE Audio must not break classic startup.
            try:
                await device.send_sync_command(
                    hci.HCI_LE_Set_Host_Feature_Command(
                        bit_number=hci.LeFeature.CONNECTED_ISOCHRONOUS_STREAM, bit_value=1
                    )
                )
                device.cis_enabled = True
            except hci.HCI_Error:
                support = replace(support, receive=False, transmit=False)
        receiver.emit(
            "le_audio_capabilities",
            receive=support.receive,
            transmit=support.transmit,
            broadcast=support.broadcast,
            transport=support.transport,
        )
        if not (support.receive or support.broadcast):
            receiver.emit(
                "warning",
                message="这个蓝牙适配器当前无法使用 LE Audio。仍可使用适配器支持的经典蓝牙音频；麦克风是否可用，以首页提示为准。",
            )
            return None
        app = cls(receiver, device, support)
        try:
            if device.keystore:
                app.bonded = bool(await device.keystore.get_all())
            await app.advertise()
            return app
        except BaseException:
            await app.close()
            raise

    async def prepare_voice(self, owner, codec):
        if self.previous_voice_prepare is not None:
            await self.previous_voice_prepare(owner, codec)
        if self.broadcast and self.broadcast.session is not None:
            await self.broadcast.stop()
            self.emit(
                "warning", message="为使用电脑麦克风，已停止音频广播。麦克风使用结束后，可重新选择播放广播。"
            )
        await self.advertise()

    def controller_failed(self, error):
        if not self.closed and not self.failure.done():
            from ..controller_health import ControllerChannelLost

            self.failure.set_result(ControllerChannelLost("蓝牙控制指令未能完成，请停止后重新开始接收。"))

    def admitted(self, owner):
        return not self.closed and self.connections.get(id(owner)) is owner

    @property
    def keys(self):
        return set(self.identities.values())

    def identity_changed(self, owner):
        identity = id(owner)
        old = self.identities.get(identity)
        fresh = str(owner.peer_address)
        if old is None or old == fresh:
            return
        already = fresh in self.receiver.connection_keys()
        self.identities[identity] = fresh
        if not already:
            self.emit("device", key=fresh, state="已连接，等待音频")
        if self.receiver.media_owner == old:
            self.receiver.media_owner = fresh
            self.receiver.emit_media_owner()
        if old in self.receiver.media_waiting:
            self.receiver.media_waiting[fresh] = self.receiver.media_waiting.pop(old)
        if old in self.receiver.policy_suspended:
            self.receiver.policy_suspended.discard(old)
            self.receiver.policy_suspended.add(fresh)
        session = self.media.get(old)
        if session is not None:
            owned = [record for record in session.records.values() if record.owner is owner]
            if owned:
                replacement = self.media.get(fresh)
                if replacement is None:
                    replacement = self.media[fresh] = LeMediaSession(self, fresh)
                replacement.format = session.format
                for record in owned:
                    session.records.pop(record.key, None)
                    record.device_key = fresh
                    replacement.records[record.key] = record
                if not session.records:
                    self.receiver.media_paused(old, session, True)
                    session.close()
                    self.media.pop(old, None)
                self.receiver.media_started(fresh, SimpleNamespace(stream=replacement), replacement)
                self.emit(
                    "format",
                    key=fresh,
                    codec="lc3",
                    rate=replacement.format.rate,
                    channels=replacement.format.channels,
                )
        source = self.bridge.source
        if source is not None and source.token.owner is owner:
            source.device_key = fresh
        if old not in self.receiver.connection_keys():
            self.emit("device", key=old, state="已断开")
        gains = self.receiver.settings.device_volumes.get(fresh, {})
        for direction in ("down", "up"):
            self.receiver.spawn_control(self.bridge.set_gain(fresh, direction, gains.get(direction, 1.0)))

    def connected(self, connection):
        if self.closed or connection.transport != PhysicalTransport.LE:
            return
        key = str(connection.peer_address)
        if (
            key not in self.receiver.connection_keys()
            and len(self.receiver.connection_keys()) >= self.receiver.settings.maximum_devices
        ):
            self.receiver.spawn_control(connection.disconnect())
            self.emit("warning", message="已达到设置的连接数量上限")
            return
        first = key not in self.receiver.connection_keys()
        self.connections[id(connection)] = connection
        self.identities[id(connection)] = key

        def lost(reason):
            self.disconnected(connection)

        def paired(keys):
            self.bonded = True
            self.identity_changed(connection)
            self.peer_controls.start(connection)
            self.service_cache.ready(connection)

        def encrypted():
            if self.admitted(connection):
                self.emit('le_audio_discovery', key=str(connection.peer_address), phase='encrypted')
                self.peer_controls.start(connection)
                self.service_cache.ready(connection)

        connection.on("disconnection", lost)
        connection.on("pairing", paired)
        connection.on('connection_encryption_change', encrypted)
        self.listeners.extend(((connection, "disconnection", lost), (connection, "pairing", paired),
                               (connection, 'connection_encryption_change', encrypted)))
        if first:
            self.emit("device", key=key, state="已连接，等待音频")
        self.emit('le_audio_discovery', key=key, phase='connected',
                  reason='已建立低功耗连接，正在等待设备配置 LC3 音频。')
        self.peer_controls.start(connection)
        self.service_cache.ready(connection)
        self.receiver.spawn_control(self.advertise())

    def disconnected(self, connection):
        if self.connections.pop(id(connection), None) is None:
            return
        self.peer_controls.disconnected(connection)
        self.service_cache.disconnected(connection)
        for source, event, callback in tuple(self.listeners):
            if source is connection:
                source.remove_listener(event, callback)
                self.listeners.remove((source, event, callback))
        key = self.identities.pop(id(connection), str(connection.peer_address))
        session = self.media.get(key)
        if session and not any(record.owner is not connection for record in session.records.values()):
            self.receiver.media_paused(key, session, True)
            session.close()
            self.media.pop(key, None)
        if self.receiver.voice_protection:
            self.receiver.voice_protection.ended(connection)
        if key not in self.receiver.connection_keys():
            self.emit("device", key=key, state="已断开")
        self.receiver.spawn_control(self.advertise())

    async def media_changed(self, record, active):
        self.identity_changed(record.owner)
        key = record.device_key
        session = self.media.get(key)
        if active:
            if session is None or session.closed:
                session = self.media[key] = LeMediaSession(self, key)
            fresh = record.key not in session.records
            session.records[record.key] = record
            session.format = Format("lc3", record.codec.rate, 2 if record.split else record.codec.channels)
            if fresh:
                self.receiver.media_started(key, SimpleNamespace(stream=session), session)
                self.emit(
                    "format",
                    key=key,
                    codec="lc3",
                    rate=session.format.rate,
                    channels=session.format.channels,
                    protocol="LE Audio",
                )
                self.emit('le_audio_discovery', key=key, phase='channel_ready',
                          reason='LE Audio 音频通道已建立，等待发送设备传来 LC3 音频。')
        elif session is not None:
            session.records.pop(record.key, None)
            if not session.records:
                self.receiver.media_paused(key, session, True)
                session.close()
                self.media.pop(key, None)
                self.receiver.update_media_gates()

    async def source_changed(self, record, active):
        self.emit(
            "call_audio",
            key=record.device_key,
            active=active,
            codec="lc3",
            rate=record.codec.rate,
            message="正在使用电脑麦克风" if active else "电脑麦克风已停止发送",
        )
        self.receiver.last_voice_at = time.monotonic()
        if not self.closed:
            await self.advertise()

    def call_snapshot(self, key):
        record = self.bridge.source
        if record is None or record.device_key != key or record.closing or not record.capture_epoch:
            return {}
        with contextlib.suppress(OSError):
            value = self.client.request("le_tx_snapshot", **self.bridge.parameters(record))
            endpoints = self.client.request("snapshot")
            receive = self.media[key]._states() if key in self.media else []
            return dict(
                microphone_active=endpoints.get("microphone_active", False),
                microphone_remote_muted=self.controls.microphone_muted,
                system_audio_active=endpoints.get("system_active", False),
                system_feedback_blocked=endpoints.get("system_feedback_blocked", False),
                active=value.get("active", False),
                codec="lc3",
                rate=record.codec.rate,
                tx_bitrate=value.get("bitrate", 0),
                rx_bitrate=sum(s.get("bitrate", 0) for s in receive),
                rx_packets=sum(s.get("packets", 0) for s in receive),
                tx_packets=value.get("packets", 0),
                frames=value.get("frames", 0),
                protocol="LE Audio",
            )
        return {}

    async def advertise(self):
        async with self.advertise_lock:
            await self._advertise()

    async def _advertise(self):
        if self.closed:
            return
        guard = self.receiver.voice_protection
        allowed = len(self.receiver.connection_keys()) < self.receiver.settings.maximum_devices and not (
            guard and guard.enabled and guard.reservation
        )
        ad = announcement(self.receiver.settings.name, receive=bool(self.bundle),
                          transmit=self.support.transmit and self.receiver.settings.calls,
                          broadcast=bool(self.bass), targeted=self.bonded,
                          tmap_roles=self.tmap_roles)
        await asyncio.wait_for(
            self.announcer.update(allowed, ad), 3
        )

    async def disconnect(self, key):
        await asyncio.gather(
            *(
                connection.disconnect()
                for connection in tuple(self.connections.values())
                if str(connection.peer_address) == key
            )
        )

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if (
            self.receiver.voice_protection
            and getattr(self.receiver.voice_protection, "before_prepare", None) == self.prepare_voice
        ):
            self.receiver.voice_protection.before_prepare = self.previous_voice_prepare
        retirement = Retirement()
        await retirement.async_call(self.peer_controls.close)
        await retirement.async_call(self.service_cache.close)
        await retirement.async_call(self.announcer.close)
        await retirement.async_call(self.controls.close)
        if self.bass:
            await retirement.async_call(self.bass.close)
        if self.broadcast:
            await retirement.async_call(self.broadcast.close)
        if self.bundle:
            await retirement.async_call(self.bundle.close)
        await retirement.async_call(self.bridge.close)
        for source, event, callback in self.listeners:
            source.remove_listener(event, callback)
        self.listeners.clear()
        for session in self.media.values():
            session.close()
        self.media.clear()
        self.connections.clear()
        self.identities.clear()
        retirement.finish(None)

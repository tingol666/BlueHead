"""Classic peer capability snapshots and stream lifetime validation.

The receiver owns physical connections; a peer owns its signalling channel and
endpoint set. Saved preferences are captured for each new connection; changing
them never reconfigures an existing stream.
"""

import asyncio
import secrets
import logging
from bumble import avdtp
from .avdtp_control import Transactions
from .protocol import format_metadata
from .codec_policy import (Selection, canonical, codec_of, offered, selections, select, validates,
                           metadata, NAMES)

log = logging.getLogger(__name__)


class CodecPeer:
    def __init__(self, receiver, protocol, session):
        self.receiver, self.protocol, self.session = receiver, protocol, session
        self.connection = protocol.l2cap_channel.connection
        self.key = str(self.connection.peer_address)
        self.transactions = Transactions(protocol)
        self.endpoints, self.remote = {}, []
        self.selection = Selection()
        self.closed, self.discovered = False, False
        self.lock = asyncio.Lock()
        self.discovery_lock = asyncio.Lock()
        self.last_cap = None
        self.session_id = secrets.token_hex(12)
        self.applying = False
        self.current_target = None
        self.preferred = canonical(receiver.settings.codec)
        self.legacy_aac_limit = receiver.settings.codec == 'aac_192'
        self.connection_policy = receiver.settings.connection_policy
        self.parameters = selections(receiver.settings)
        self.protocol.on('close', self.close)

    def current(self):
        return (not self.closed and not self.receiver.retiring and not self.session.closed
                and self.receiver.connections.get(self.key) is self.connection
                and self.receiver.codec_peers.get(self.key) is self)

    def close(self):
        self.closed = True
        self.transactions.close()

    def check_change(self):
        if not self.current():
            raise ConnectionError('设备已断开，编码调整已停止')
        if self.receiver.voice_in_use():
            raise ConnectionError('正在通话或录音，媒体编码调整已停止')

    def limit(self, codec, selection=None):
        return offered(codec, selection or self.parameters.get(codec), legacy_aac_limit=self.legacy_aac_limit)

    def configuration(self, codec, config):
        if not self.current():
            raise ConnectionError('设备已断开，忽略过期的编码配置')
        return validates(codec, config, self.limit(codec))

    def watch_stream(self, stream):
        if stream is None or getattr(stream, '_bluehead_lifetime', False):
            return
        stream._bluehead_lifetime = True
        old_open, old_close = stream.on_l2cap_channel_open, stream.on_l2cap_channel_close

        def opened():
            if self.current() and stream.local_endpoint.stream is stream:
                old_open()

        def closed():
            if self.current() and stream.local_endpoint.stream is stream:
                old_close()
            else:
                # A delayed callback from a replaced stream must not unroute or
                # pause its successor through the shared LocalSink endpoint.
                stream.rtp_channel = None
                stream.change_state(avdtp.State.IDLE)

        stream.on_l2cap_channel_open, stream.on_l2cap_channel_close = opened, closed

    def configured(self, codec, config):
        self.last_cap = next(cap for cap in config if isinstance(cap, avdtp.MediaCodecCapabilities))
        self.session.connection_details = format_metadata(self.session.format, config)
        actual = dict(codec=codec, rate=self.session.format.rate,
                      **self.session.connection_details)
        reason = ''
        selection = self.parameters.get(codec, Selection(codec))
        expected = self.preferred
        if expected != 'auto' and codec != expected:
            reason = f'发送设备选择了 {NAMES[codec]}，没有采用首选 {NAMES.get(expected, expected)}。'
        if self.connection_policy == 'prefer_le':
            reason = (reason + ' ' if reason else '') + '当前使用经典蓝牙；发送设备尚未建立可播放的 LE Audio 音频。'
        self.receiver.emit('codec_state', key=self.key, phase='configured', actual=actual,
                           requested=selection.wire(), reason=reason)

    async def discover(self):
        async with self.discovery_lock:
            await self._discover()

    async def _discover(self):
        if self.discovered:
            return
        async with asyncio.timeout(6):
            response = await self.protocol.send_command(avdtp.Discover_Command())
            entries = list(response.endpoints)
            if len(entries) > 62 or len({entry.seid for entry in entries}) != len(entries):
                raise ValueError('设备返回的媒体端点列表无效')
            found = []
            for entry in entries:
                if entry.tsep != avdtp.AVDTP_TSEP_SRC or entry.media_type != avdtp.MediaType.AUDIO:
                    continue
                if not self.current():
                    raise ConnectionError('设备已断开')
                response = await self.protocol.get_capabilities(entry.seid)
                for cap in response.capabilities:
                    if isinstance(cap, avdtp.MediaCodecCapabilities):
                        codec = codec_of(cap)
                        if codec in self.endpoints:
                            endpoint = avdtp.DiscoveredStreamEndPoint(
                                self.protocol, entry.seid, entry.media_type, entry.tsep,
                                entry.in_use, response.capabilities)
                            found.append((codec, endpoint, cap))
            self.remote, self.discovered = found, True

    def candidates(self, codec, selection):
        local = self.limit(codec, selection)
        for name, endpoint, cap in self.remote:
            if name == codec:
                try:
                    yield endpoint, select(codec, local, cap, selection)
                except ValueError:
                    continue

    async def options(self):
        if not self.current():
            raise ConnectionError('设备已断开')
        if self.lock.locked():
            raise ValueError('这台设备正在调整编码，请等待完成后再读取')
        unknown = ''
        try:
            await self.discover()
        except Exception:
            unknown = '发送设备没有完整公布编码能力；可查看当前结果，或在发送设备上切换。'
        if not self.current():
            raise ConnectionError('设备已断开')
        rows = []
        for name in self.endpoints:
            base = metadata(name)
            compatible = list(self.candidates(name, Selection(name)))
            rates = [rate for rate in base['rates'] if any(
                self.candidates(name, Selection(name, rate=rate)))]
            depths = [depth for depth in base['depths'] if any(
                self.candidates(name, Selection(name, depth=depth)))]
            base.update(supported=bool(compatible), rates=rates, depths=depths,
                        reason='' if compatible else unknown or '发送设备没有提供双方可用的这个编码格式。')
            rows.append(base)
        return dict(codecs=rows, selection=self.selection.wire(), session_id=self.session_id,
                    actual={'codec': self.session.format.codec, 'rate': self.session.format.rate,
                            'depth': self.session.format.depth} if self.session.format else {},
                    temporary=True, reason=unknown, audio_transport='classic')

    def progress(self, phase, message, request_id):
        self.receiver.emit('codec_switch', key=self.key, phase=phase, message=message,
                           request_id=request_id, done=False)

    async def configure_native(self, endpoint, cap):
        self.check_change()
        result = await endpoint.on_set_configuration_command([
            avdtp.ServiceCapabilities(avdtp.AVDTP_MEDIA_TRANSPORT_SERVICE_CATEGORY), cap])
        if result is not None:
            raise ValueError('接收端未能准备所选音频格式')
        self.check_change()

    async def suspend(self, endpoint):
        stream = endpoint.stream
        if stream and stream.state == avdtp.State.STREAMING:
            await stream.remote_endpoint.stop()
            if not self.current():
                raise ConnectionError('设备已断开')
            await stream.on_suspend_command()

    async def resume(self, endpoint):
        self.check_change()
        stream = endpoint.stream
        if stream and stream.state == avdtp.State.OPEN:
            await stream.start()
            self.check_change()
            await endpoint.on_start_command()

    async def open_stream(self, name, remote, cap):
        endpoint = self.endpoints[name]
        self.current_target = endpoint
        await self.configure_native(endpoint, cap)
        self.check_change()
        # Bumble's initiator methods are usable for a sink too, provided the
        # created media channel is explicitly wired to LocalSink afterwards.
        stream = avdtp.Stream(self.protocol, endpoint, remote)
        self.watch_stream(stream)
        self.protocol.streams[endpoint.seid] = stream
        await stream.configure()
        self.check_change()
        await stream.open()
        self.check_change()
        channel = stream.rtp_channel
        stream.on_l2cap_connection(channel)
        stream.on_l2cap_channel_open()
        return endpoint

    async def release(self, endpoint):
        if endpoint is None or endpoint.stream is None:
            return
        stream = endpoint.stream
        if stream.state in (avdtp.State.OPEN, avdtp.State.STREAMING):
            await stream.close()
        elif stream.state == avdtp.State.CONFIGURED:
            await stream.remote_endpoint.abort()
            stream.change_state(avdtp.State.IDLE)
        if hasattr(self.session, 'unbind'):
            self.session.unbind()
        self.session.pause()

    async def change(self, name, remote, cap, *, was_playing, old_endpoint=None):
        current = old_endpoint
        if current:
            await self.suspend(current)
        self.check_change()
        same = current is self.endpoints[name] and current.stream and current.stream.state == avdtp.State.OPEN
        if same:
            self.current_target = current
            try:
                await self.protocol.send_command(avdtp.Reconfigure_Command(
                    current.stream.remote_endpoint.seid, [cap]))
            except avdtp.ProtocolError:
                # Some sources reject RECONFIGURE but accept a fresh stream.
                await self.release(current)
                current = await self.open_stream(name, remote, cap)
            else:
                self.check_change()
                await self.configure_native(current, cap)
        else:
            await self.release(current)
            current = await self.open_stream(name, remote, cap)
        self.session._policy_sink = current
        if self.receiver.media_sessions().get(self.key) is self.session:
            self.receiver.media_sinks[self.key] = current
        if was_playing:
            await self.resume(current)
        return current

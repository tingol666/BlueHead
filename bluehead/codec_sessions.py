"""Per-connection Classic codec operations; no persistent device overrides.

The receiver owns physical connections; a peer owns its signalling channel and
endpoint set. Switching never restarts the controller or any other audio source.
"""

import asyncio
import secrets
import logging
from bumble import avdtp
from .avdtp_control import Transactions
from .codec_policy import (Selection, canonical, codec_of, offered, select, validates,
                           metadata, ordered, NAMES)

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
        return offered(codec, selection, legacy_aac_limit=self.legacy_aac_limit)

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
        actual = dict(codec=codec, rate=self.session.format.rate,
                      depth=getattr(self.session.format, 'depth', 0))
        reason = ''
        expected = self.selection.codec if self.selection.codec != 'auto' else self.preferred
        if expected != 'auto' and codec != expected:
            reason = f'发送设备选择了 {NAMES[codec]}，没有采用首选 {NAMES.get(expected, expected)}。'
        elif self.selection.rate and actual['rate'] != self.selection.rate:
            reason = '发送设备重新选择了采样率，当前显示的是实际协商结果。'
        if self.receiver.settings.connection_policy == 'prefer_le':
            reason = (reason + ' ' if reason else '') + '当前使用经典蓝牙；发送设备尚未建立可播放的 LE Audio 音频。'
        self.receiver.emit('codec_state', key=self.key, phase='configured', actual=actual,
                           requested=self.selection.wire(), reason=reason)

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

    async def switch(self, selection, request_id):
        if self.lock.locked():
            raise ValueError('这台设备正在调整编码，请等待当前操作完成')
        async with self.lock:
            if not self.current():
                raise ConnectionError('设备已断开')
            if self.receiver.voice_in_use():
                raise ValueError('正在通话或录音，请结束后再调整媒体编码')
            self.progress('checking', '正在核对这台设备支持的编码…', request_id)
            await self.discover()
            names = [selection.codec] if selection.codec != 'auto' else ordered(self.receiver.settings)
            if selection.codec == 'auto':
                self.preferred = canonical(self.receiver.settings.codec)
            choices = [(name, remote, cap) for name in names if name in self.endpoints
                       for remote, cap in self.candidates(name, Selection(
                           name, selection.rate, selection.depth, selection.bitrate or (
                               192000 if name == 'aac' and selection.codec == 'auto'
                               and self.receiver.settings.codec == 'aac_192' else 0), selection.quality))]
            if not choices:
                raise ValueError('双方没有提供所选编码和参数。请换一个选项，或在发送设备上选择。')
            name, remote, cap = choices[0]
            old_endpoint = self.receiver.media_sinks.get(self.key)
            old_cap, previous = self.last_cap, self.selection
            old_name = self.session.format.codec if self.session.format else None
            old_remote = old_endpoint.stream.remote_endpoint if old_endpoint and old_endpoint.stream else None
            playing = not self.session.paused
            policy_paused = self.key in self.receiver.policy_suspended
            waiting_at = self.receiver.media_waiting.get(self.key)
            self.receiver.codec_switching.add(self.key)
            self.progress('switching', f'正在切换为 {NAMES[name]}，声音会短暂中断…', request_id)
            self.selection = selection
            self.applying = True
            self.current_target = old_endpoint
            try:
                async with asyncio.timeout(9):
                    await self.change(name, remote, cap, was_playing=playing, old_endpoint=old_endpoint)
                if not self.current():
                    raise ConnectionError('设备已断开')
                self.configured(name, [cap])
                self.receiver.emit('codec_switch', key=self.key, request_id=request_id, done=True, success=True,
                                   phase='applied', selection=selection.wire(),
                                   actual={'codec': name, 'rate': self.session.format.rate,
                                           'depth': self.session.format.depth},
                                   message='本次连接的编码已调整。' if playing else '格式已配置，请在发送设备上继续播放。')
            except BaseException as error:
                if not isinstance(error, asyncio.CancelledError):
                    log.warning('Device codec change failed (%s)', type(error).__name__, exc_info=True)
                self.selection = previous
                restored = bool(self.current() and old_endpoint and old_endpoint.stream
                    and old_endpoint.stream.state == avdtp.State.STREAMING
                    and self.current_target is old_endpoint and not self.session.paused
                    and self.session.format and self.session.format.codec == old_name
                    and self.last_cap is old_cap)
                if not restored and self.current() and not self.receiver.voice_in_use() and old_cap and old_remote:
                    self.progress('restoring', '切换未完成，正在恢复原来的媒体格式…', request_id)
                    try:
                        async with asyncio.timeout(7):
                            now = self.current_target
                            await self.change(old_name, old_remote, old_cap, was_playing=playing, old_endpoint=now)
                            restored = True
                    except Exception:
                        log.warning('Device media format rollback failed', exc_info=True)
                if isinstance(error, asyncio.CancelledError):
                    raise
                message = ('未能完成这次调整，原格式仍可继续播放。' if restored else
                           '已停止媒体编码调整。请结束通话或录音后继续播放；其它设备无需操作。'
                           if self.receiver.voice_in_use() else
                           '设备未能完成编码切换。请在该设备上断开 BlueHead 后重新连接；其它设备无需操作。')
                self.receiver.emit('codec_switch', key=self.key, request_id=request_id, done=True,
                                   phase='failed', success=False, restored=restored,
                                   message=message, error=type(error).__name__, stale=not self.current())
            finally:
                self.applying = False
                self.receiver.codec_switching.discard(self.key)
                endpoint = self.receiver.media_sinks.get(self.key)
                if (self.current() and policy_paused and self.receiver.settings.media_mode == 'single'
                        and self.receiver.media_owner != self.key and endpoint and endpoint.stream
                        and endpoint.stream.state in (avdtp.State.OPEN, avdtp.State.STREAMING)):
                    self.receiver.policy_suspended.add(self.key)
                    if waiting_at is not None:
                        self.receiver.media_waiting.setdefault(self.key, waiting_at)


async def execute(receiver, operation, values):
    key, request_id = values['key'], values['request_id']
    peer = receiver.codec_peers.get(key)
    try:
        if (receiver.le_audio and key in receiver.le_audio.media
                and receiver.media_sessions().get(key) is receiver.le_audio.media[key]):
            if operation == 'codec_switch':
                raise ValueError('当前使用 LE Audio / LC3，音频参数由发送设备协商。切换经典编码需先更改连接与编码设置。')
            session = receiver.le_audio.media[key]
            receiver.emit('codec_options', key=key, request_id=request_id, success=True,
                          audio_transport='le', codecs=[], temporary=True,
                          actual={'codec': 'lc3', 'rate': session.format.rate},
                          reason='当前使用 LE Audio / LC3；采样率、帧配置与发送质量由发送设备协商。')
            return
        if peer is None or not peer.current():
            raise ValueError('这个设备尚未建立可调整的经典媒体连接，请先播放一次媒体')
        if operation == 'codec_options':
            receiver.emit('codec_options', key=key, request_id=request_id, success=True, **await peer.options())
        else:
            if values.get('session_id') != peer.session_id:
                raise ValueError('这台设备已重新连接，请关闭调整窗口后从设备的“…”重新打开')
            await peer.switch(Selection.parse(values['selection']), request_id)
    except Exception as error:
        receiver.emit(operation, key=key, request_id=request_id, success=False, done=True,
                      phase='failed', message=str(error), stale=peer is not None and not peer.current())


def validate(message, *, switch=False):
    key, request_id = message.get('key'), message.get('request_id')
    if not isinstance(key, str) or not 1 <= len(key) <= 200:
        raise ValueError('无效设备标识')
    if type(request_id) is not int or not 0 <= request_id <= 0x7fffffff:
        raise ValueError('无效操作编号')
    result = dict(key=key, request_id=request_id)
    if switch:
        result['selection'] = Selection.parse(message.get('selection')).wire()
        session = message.get('session_id')
        if not isinstance(session, str) or len(session) != 24 or any(c not in '0123456789abcdef' for c in session):
            raise ValueError('无效连接编号，请重新打开设备的编码与音质窗口')
        result['session_id'] = session
    return result

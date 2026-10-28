"""HFP hands-free and explicitly selected duplex microphone routing.

Adapted from google/bumble examples/run_hfp_handsfree.py, Apache License 2.0.
Copyright 2021-2026 Google LLC. Modified for per-device isolation and live PCM.
"""
import asyncio
import contextlib
from dataclasses import replace
import logging
import threading

from bumble import hci, hfp, rfcomm
from bumble.core import PhysicalTransport

from .audio import SwitchableStream, resolve_endpoint
from .audio_runtime import on_audio_thread
from .metrics import TrafficMeter
from .uplink import CallRouter
from .voice import VoiceCodec, available_codecs
from .hfp_compat import HandsFreeProtocol, handsfree_records, selected_codec, renegotiate_fallback
from .errors import Retirement

log = logging.getLogger(__name__)
MICROPHONE_OPEN_TIMEOUT = 5


class CaptureRoute:
    """An unopened or obsolete microphone must not write into a later call."""
    def __init__(self, router):
        self.router = router
        self.active = False
        self.lock = threading.Lock()

    def activate(self):
        with self.lock:
            self.active = True

    def close(self):
        with self.lock:
            self.active = False

    def write_microphone(self, key, data):
        with self.lock:
            if self.active:
                self.router.write_microphone(key, data)

    def write_system(self, key, data):
        with self.lock:
            if self.active:
                self.router.write_system(key, data)

    def clear(self, key):
        self.router.clear(key)


class PendingCapture:
    """Keep ownership when a non-cancellable OS open finishes after hangup."""
    def __init__(self, factory):
        self.factory = factory
        self.lock = threading.Lock()
        self.abandoned = False
        self.microphone = None

    def start(self):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        def finished(error):
            if not future.done():
                if error is None:
                    future.set_result(None)
                else:
                    future.set_exception(error)
        def run():
            error = None
            try:
                self.open()
            except Exception as exc:
                error = exc
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(finished, error)
        # A stuck audio driver must not keep the receiver or application alive.
        threading.Thread(target=run, daemon=True, name="microphone-open").start()
        return future

    def open(self):
        with self.lock:
            if self.abandoned:
                return
        microphone = self.factory()
        with self.lock:
            if not self.abandoned:
                self.microphone = microphone
                return
        microphone.close()

    def take(self):
        with self.lock:
            self.abandoned = True
            microphone, self.microphone = self.microphone, None
            return microphone


class Microphone(SwitchableStream):
    @on_audio_thread
    def __init__(self, router, key, rate, device, emit):
        from .capture import CaptureStream
        self.router, self.key, self.emit = router, key, emit

        def create(selected):
            return CaptureStream(resolve_endpoint(selected, "input"), rate,
                lambda data: self.router.write_microphone(self.key, data), emit)

        super().__init__(create, device, "input")
        try:
            self.start()
        except Exception:
            self.close()
            raise

    def switch_device(self, device, force=False):
        super().switch_device(device, force=force)
        self.router.clear(self.key)

    @property
    def frames(self):
        return self.stream.snapshot().get("input_frames", 0)

    @property
    def overflows(self):
        return self.stream.snapshot().get("status_events", 0)


class CallManager:
    def __init__(self, device, mixer, emit, input_device=None, *, router=None):
        self.device, self.mixer, self.emit = device, mixer, emit
        self.input_device = input_device
        self.router = router if router is not None else CallRouter()
        self.protocols, self.links, self.tasks = {}, {}, set()
        self.protocol_tasks = {}
        self.connections, self.clients, self.control_ready, self.control_locks = {}, {}, {}, {}
        self.closed = False
        self.voice_protection = None
        self.microphone = None
        self.capture_route = None
        self.capture_tasks = {}
        self.cleanup_tasks = set()
        self.input_lock = asyncio.Lock()
        self.statistics = {}
        self.traffic = {}
        self.voice_buffers = {}
        self.voice_codecs = {}
        self.downlink_gains, self.speaker_volumes = {}, {}
        self.system_enabled = False
        self.system_capture = self.system_route = None
        self.system_capture_tasks = {}
        self.system_lock = asyncio.Lock()
        self.configuration = hfp.HfConfiguration(
            supported_hf_features=[
                hfp.HfFeature.REMOTE_VOLUME_CONTROL,
                hfp.HfFeature.CODEC_NEGOTIATION,
                hfp.HfFeature.ESCO_S4_SETTINGS_SUPPORTED,
            ],
            supported_hf_indicators=[],
            supported_audio_codecs=[hfp.AudioCodec(codec) for codec in available_codecs()],
        )
        self.server = rfcomm.Server(device)
        channel = self.server.listen(self.on_dlc)
        self.sdp_record = handsfree_records(0x10002, channel, self.configuration)
        device.sdp_service_records[0x10002] = self.sdp_record
        device.on("connection", self.on_connection)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        def done(t):
            self.tasks.discard(t)
            if not t.cancelled() and t.exception():
                self.emit("error", message=f"语音操作失败：{t.exception()}")
        task.add_done_callback(done)
        return task

    def snapshot(self, key):
        link = self.links.get(key)
        if link is None:
            return {}
        stats = self.statistics.get(key, {})
        traffic = self.traffic.get(key, {})
        system_capture = self.system_capture
        codec = self.voice_codecs.get(key)
        voice = {}
        if codec is not None:
            try:
                voice = codec.stats()
            except (RuntimeError, OSError):
                # The UI polls from another thread; hangup may close this
                # context after we capture it. A retired snapshot is absent.
                pass
        result = {"active": True, "codec": stats.get("codec", ""), "rate": stats.get("rate", 0),
                "channels": 1, "channel_mode": "mono", "depth": 0, "depth_status": "not_signalled",
                "rx_bitrate": traffic["rx"].snapshot()["bitrate"] if traffic else 0,
                "tx_bitrate": traffic["tx"].snapshot()["bitrate"] if traffic else 0,
                "rx_packets": stats.get("rx_packets", 0), "decode_errors": stats.get("decode_errors", 0),
                "empty_packets": stats.get("empty_packets", 0),
                "uplink_clock": self.router.snapshot(),
                "voice": voice,
                "microphone_active": self.microphone is not None,
                "system_audio_active": system_capture is not None and system_capture.thread.is_alive()}
        return result if self.links.get(key) is link else {}

    def close_system_capture(self):
        for task in list(self.system_capture_tasks.values()):
            task.cancel()
        self.system_capture_tasks.clear()
        if self.system_route:
            self.system_route.close()
            self.system_route = None
        if self.system_capture:
            self.retire_microphone(self.system_capture)
            self.system_capture = None
        self.router.clear_system()

    async def open_system_capture(self, key, link, rate):
        from .loopback import SystemCapture
        route = CaptureRoute(self.router)
        job = None
        capture = None
        try:
            async with self.system_lock:
                if self.closed or not self.system_enabled or self.links.get(key) is not link:
                    return
                job = PendingCapture(lambda: SystemCapture(route, key, rate, self.emit))
                future = job.start()
                future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
                await asyncio.wait_for(asyncio.shield(future), 8)
                capture = job.take()
                if self.closed or not self.system_enabled or self.links.get(key) is not link:
                    return
                self.system_capture, self.system_route = capture, route
                capture = None
                route.activate()
                self.emit("system_audio_state", active=True, message="电脑声音已加入发送，已排除移动设备返听")
        except Exception as error:
            self.emit("system_audio_state", active=False, message=f"电脑声音未加入发送，麦克风继续使用：{error}")
        finally:
            if job:
                late = job.take()
                if late:
                    self.retire_microphone(late)
            if capture:
                self.retire_microphone(capture)
            if self.system_route is not route:
                route.close()
            if self.system_capture_tasks.get(key) is asyncio.current_task():
                self.system_capture_tasks.pop(key, None)

    async def set_system_source(self, endpoint):
        if endpoint is not None:
            raise OSError("指定电脑声音设备需要使用原生音频引擎")

    async def set_system_audio(self, enabled):
        self.system_enabled = enabled
        self.close_system_capture()
        key = self.router.owner
        link = self.links.get(key)
        if enabled and link:
            task = self.system_capture_tasks[key] = self.spawn(self.open_system_capture(key, link, 48000))
            await asyncio.shield(task)
            if self.links.get(key) is link and self.system_capture is None:
                self.system_enabled = False
                raise OSError("电脑声音采集未就绪，已保留麦克风模式")

    def on_connection(self, connection):
        if (self.closed or connection.transport != PhysicalTransport.BR_EDR or
                self.voice_protection and not self.voice_protection.allowed(connection)):
            return
        key = str(connection.peer_address)
        previous = self.connections.get(key)
        if previous is connection:
            return
        if previous is not None:
            self.retire_connection(key, previous)
        self.connections[key] = connection

        async def connect_control():
            # Let the phone initiate first; otherwise behave like a headset and
            # establish the service-level connection from this side.
            await asyncio.sleep(2)
            if not self.closed and self.connections.get(key) is connection:
                try:
                    await self.ensure_control(key)
                except Exception as exc:
                    log.warning("HFP connect failed: peer=%s %s", key, exc)
                    self.emit("call", key=key, state="蓝牙语音服务未连接，可点击“连接语音通道”重试")

        task = self.spawn(connect_control())

        def disconnected(reason):
            task.cancel()
            self.retire_connection(key, connection)
        connection.once("disconnection", disconnected)

    def retire_connection(self, key, connection):
        """ACL validity owns all subordinate profiles, even with missing events."""
        if self.connections.get(key) is not connection:
            return
        self.connections.pop(key, None)
        if self.voice_protection:
            self.voice_protection.ended(connection)
        retirement = Retirement()
        protocol = self.protocols.pop(key, None)
        self.control_ready.pop(key, None)
        task = self.protocol_tasks.pop(key, None)
        if task and task is not asyncio.current_task():
            retirement.call(task.cancel)
        if protocol:
            retirement.call(protocol.stop)
        old_link = self.links.get(key)
        retirement.call(self.audio_closed, key)
        if old_link is not None:
            self.spawn(self.disconnect_voice(old_link))
        client = self.clients.pop(key, None)
        if client:
            retirement.call(self.retire_rfcomm_client, client)
        self.control_locks.pop(key, None)
        self.statistics.pop(key, None)
        self.traffic.pop(key, None)
        self.speaker_volumes.pop(key, None)
        retirement.call(self.router.device_standard_gains.pop, key, None)
        self.downlink_gains.pop(key, None)
        # As with the media disconnect listener, failures must not prevent the
        # remaining profile listeners on the same ACL from running.
        if retirement.first:
            log.warning("Disconnected voice cleanup incomplete (%s)", type(retirement.first).__name__)

    async def ensure_control(self, key):
        connection = self.connections.get(key)
        if connection is None:
            raise ValueError("移动设备已经断开")
        lock = self.control_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if self.closed or self.connections.get(key) is not connection:
                raise ConnectionError("蓝牙连接已更换")
            if key not in self.protocols:
                self.emit("call", key=key, state="正在连接蓝牙语音服务…")
                async with asyncio.timeout(12):
                    record = await hfp.find_ag_sdp_record(connection)
                    if self.closed or self.connections.get(key) is not connection:
                        raise ConnectionError("蓝牙连接已更换")
                    if key not in self.protocols:
                        if record is None:
                            raise ValueError("移动设备没有提供 HFP 网关服务")
                        client = rfcomm.Client(connection)
                        transferred = False
                        try:
                            mux = await client.start()
                            if self.closed or self.connections.get(key) is not connection:
                                raise ConnectionError("蓝牙连接已更换")
                            if key not in self.protocols:
                                dlc = await mux.open_dlc(record[0])
                                if self.closed or self.connections.get(key) is not connection:
                                    raise ConnectionError("蓝牙连接已更换")
                                # An incoming SLC can win while open_dlc waits.
                                # It owns the peer slot; retire only our new mux.
                                if key not in self.protocols:
                                    self.clients[key] = client
                                    self.on_dlc(dlc)
                                    transferred = True
                        finally:
                            if not transferred:
                                if self.clients.get(key) is client:
                                    self.clients.pop(key, None)
                                self.retire_rfcomm_client(client)
            protocol = self.protocols.get(key)
            if protocol is None:
                raise RuntimeError("蓝牙语音服务未连接")
            ready = self.control_ready[key]
            await asyncio.wait_for(ready.wait(), 12)
            if (not protocol._slc_initialized or self.connections.get(key) is not connection
                    or self.protocols.get(key) is not protocol):
                raise RuntimeError("蓝牙语音服务协商未完成")
            return protocol

    def on_dlc(self, dlc):
        connection = dlc.multiplexer.l2cap_channel.connection
        key = str(connection.peer_address)
        if self.closed or self.connections.get(key, connection) is not connection:
            self.spawn(self.disconnect_dlc(dlc))
            return
        self.connections[key] = connection
        if key in self.protocols:
            self.spawn(self.disconnect_dlc(dlc))
            return
        protocol = HandsFreeProtocol(dlc, self.configuration)
        async def before_codec(codec):
            return (not self.closed and self.connections.get(key) is connection and
                    (self.voice_protection is None or await self.voice_protection.prepare(connection, codec)))
        protocol.before_codec = before_codec
        self.protocols[key] = protocol
        def current():
            return (not self.closed and self.connections.get(key) is connection and
                    self.protocols.get(key) is protocol and not protocol.stopped)
        ready = self.control_ready[key] = asyncio.Event()
        async def run_protocol():
            try:
                await asyncio.wait_for(protocol.initiate_slc(), 12)
                ready.set()
                if not current():
                    return
                self.emit("call", key=key, state="蓝牙语音服务已就绪")
                log.info("HFP service ready: %s", key)
                await protocol.run()
            except Exception as error:
                # Malformed AT parameters must not be echoed into logs/UI.
                log.warning("HFP service stopped (%s)", type(error).__name__)
                if not self.closed and self.protocols.get(key) is protocol:
                    self.emit("call", key=key, state="蓝牙语音服务已停止，可重新连接")
            finally:
                ready.set()
                # Bumble's run() can return after a protocol error. Never leave
                # a dead session registered as a usable headset connection.
                await self.disconnect_dlc(dlc)
        self.protocol_tasks[key] = self.spawn(run_protocol())
        def request(link_type):
            if current():
                self.spawn(self.accept_audio(key, protocol, connection, link_type))
        def linked(link):
            if current():
                guard = self.voice_protection
                if guard and not guard.activate(connection, link, selected_codec(protocol)):
                    self.spawn(self.disconnect_voice(link))
                    guard.failed(connection)
                    return
                self.on_link(key, protocol, link)
                if guard and self.links.get(key) is not link:
                    guard.ended(connection, link)
            else:
                self.spawn(self.disconnect_voice(link))
        def failed(status):
            if not current():
                return
            if self.voice_protection:
                self.voice_protection.failed(connection)
            log.warning("SCO connection failed: peer=%s status=%s", key, status)
            self.emit("call", key=key, state=f"语音连接失败 · HCI 0x{status:02X}")
            if status in (hci.HCI_UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
                          hci.HCI_INVALID_HCI_COMMAND_PARAMETERS_ERROR, hci.HCI_UNSUPPORTED_REMOTE_FEATURE_ERROR):
                self.spawn(self.codec_fallback(key, protocol, selected_codec(protocol)))
        connection.on("sco_request", request)
        connection.on("sco_connection", linked)
        connection.on("sco_connection_failure", failed)
        protocol.on("codec_negotiation", lambda codec: log.info("HFP codec: peer=%s codec=%s", key, codec.name))
        def codec_failed(codec):
            if self.voice_protection and current():
                self.voice_protection.failed(connection)
            link = self.links.get(key) if current() else None
            if link is not None:
                self.spawn(self.disconnect_voice(link))
        protocol.on("codec_failed", codec_failed)
        protocol.on(protocol.EVENT_SPEAKER_VOLUME, lambda value: current() and self.remote_volume(key, value, "speaker"))
        protocol.on(protocol.EVENT_MICROPHONE_VOLUME, lambda value: current() and self.remote_volume(key, value, "microphone"))
        protocol.on("ag_indicator", lambda indicator: current() and self.indicator_changed(key, protocol, indicator))
        protocol.on("ring", lambda: current() and self.emit("call", key=key, state="来电", phase="incoming"))
        cleaned = False
        def closed():
            nonlocal cleaned
            if cleaned:
                return
            cleaned = True
            retirement = Retirement()
            connection.remove_listener("sco_request", request)
            connection.remove_listener("sco_connection", linked)
            connection.remove_listener("sco_connection_failure", failed)
            if self.protocols.get(key) is protocol:
                if self.voice_protection:
                    self.voice_protection.failed(connection)
                old_link = self.links.get(key)
                self.protocols.pop(key, None)
                self.protocol_tasks.pop(key, None)
                self.control_ready.pop(key, None)
                retirement.call(self.audio_closed, key)
                if old_link is not None:
                    self.spawn(self.disconnect_voice(old_link))
                self.emit("call", key=key, state="蓝牙语音服务已断开，可重连")
            retirement.call(protocol.stop)
            client = self.clients.get(key)
            if client is not None and client.multiplexer is dlc.multiplexer:
                self.clients.pop(key, None)
                retirement.call(self.retire_rfcomm_client, client)
        protocol.on("protocol_error", lambda reason: self.spawn(self.disconnect_dlc(dlc)))
        dlc.once("close", closed)
        dlc.multiplexer.l2cap_channel.once("close", closed)
        self.emit("call", key=key, state="正在建立蓝牙语音服务…")
        log.info("HFP RFCOMM connected: %s", key)

    async def codec_fallback(self, key, protocol, failed_codec):
        if self.closed or self.protocols.get(key) is not protocol or key in self.links:
            return
        result = await renegotiate_fallback(protocol, failed_codec=failed_codec)
        if self.closed or self.protocols.get(key) is not protocol or key in self.links:
            return
        if result.requested:
            codecs = "/".join(codec.name for codec in result.offered_codecs)
            self.emit("call", key=key, state=f"正在回退语音编码 · {codecs}，等待设备重新协商")
        elif result.reconnect_required:
            self.emit("call", key=key, state="语音协商超时，请重新连接设备的蓝牙语音服务")
            await self.disconnect_dlc(protocol.dlc)
        elif result.status not in ("busy", "stale"):
            self.emit("call", key=key, state="语音编码回退未完成，可重新连接设备后重试")

    def set_downlink_gain(self, key, gain):
        self.downlink_gains[key] = gain
        self.mixer.set_gain("call:" + key, gain * self.speaker_volumes.get(key, 1.0))

    def remote_volume(self, key, value, direction):
        if type(value) is not int or not 0 <= value <= 15:
            return
        if direction == "speaker":
            self.speaker_volumes[key] = value / 15
            self.set_downlink_gain(key, self.downlink_gains.get(key, self.mixer.gains.get("call:" + key, 1.0)))
        else:
            self.router.device_standard_gains[key] = value / 15
        self.emit("voice_volume", key=key, direction=direction, volume=value / 15)

    def indicator_changed(self, key, protocol, indicator):
        if indicator.indicator not in (hfp.AgIndicator.CALL, hfp.AgIndicator.CALL_SETUP, hfp.AgIndicator.CALL_HELD):
            return
        values = {item.indicator: item.current_status for item in protocol.ag_indicators}
        if values.get(hfp.AgIndicator.CALL, 0):
            state, phase = "通话中", "active"
        elif values.get(hfp.AgIndicator.CALL_SETUP, 0) == 1:
            state, phase = "来电", "incoming"
        elif values.get(hfp.AgIndicator.CALL_SETUP, 0) in (2, 3):
            state, phase = "正在拨号 · 等待接听", "outgoing"
        else:
            state, phase = "蓝牙语音服务已就绪", "ready"
        self.emit("call", key=key, state=state, phase=phase)

    async def accept_audio(self, key, protocol, connection, link_type):
        if (self.closed or getattr(protocol, "stopped", False)
                or self.protocols.get(key, protocol) is not protocol
                or self.connections.get(key, connection) is not connection):
            await self.device.send_async_command(hci.HCI_Reject_Synchronous_Connection_Request_Command(
                bd_addr=connection.peer_address, reason=hci.HCI_ErrorCode.CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES_ERROR))
            return
        if self.router.owner and self.router.owner != key and self.links:
            await self.device.send_async_command(hci.HCI_Reject_Synchronous_Connection_Request_Command(
                bd_addr=connection.peer_address, reason=hci.HCI_ErrorCode.CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES_ERROR,
            ))
            self.emit("warning", key=key, message="另一设备正在使用语音通道，请先切换发送设备")
            return
        codec = selected_codec(protocol)
        if self.voice_protection and not await self.voice_protection.prepare(connection, codec):
            await self.device.send_async_command(hci.HCI_Reject_Synchronous_Connection_Request_Command(
                bd_addr=connection.peer_address, reason=hci.HCI_ErrorCode.CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES_ERROR))
            return
        # Disconnect/preflight awaits must not revive a replaced protocol.
        if (self.closed or getattr(protocol, "stopped", False) or
                self.protocols.get(key, protocol) is not protocol or
                self.connections.get(key, connection) is not connection):
            if self.voice_protection:
                self.voice_protection.failed(connection)
            return
        transparent = codec in (hfp.AudioCodec.MSBC, hfp.AudioCodec.LC3_SWB)
        log.info("SCO request: peer=%s link_type=%s codec=%s", key, link_type, protocol.active_codec)
        if link_type == hci.HCI_Connection_Complete_Event.LinkType.SCO:
            if transparent:
                # Compressed wideband codecs require eSCO. A different codec
                # must be negotiated with AT+BCS, never silently relabelled.
                await self.device.send_async_command(hci.HCI_Reject_Synchronous_Connection_Request_Command(
                    bd_addr=connection.peer_address, reason=hci.HCI_ErrorCode.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR))
                self.emit("call", key=key, state="宽带语音需要 eSCO，等待设备重新协商")
                await self.codec_fallback(key, protocol, codec)
                return
            parameters = hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.SCO_CVSD_D1]
        elif transparent:
            parameters = replace(
                hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.ESCO_MSBC_T2],
                # The host encodes mSBC including H2 headers. All four coding
                # formats must be transparent; MSBC on air requests a controller
                # codec instead. See Linux hci_conn.c BT_CODEC_TRANSPARENT.
                transmit_coding_format=hci.CodingFormat(hci.CodecID.TRANSPARENT),
                receive_coding_format=hci.CodingFormat(hci.CodecID.TRANSPARENT),
                input_coding_format=hci.CodingFormat(hci.CodecID.TRANSPARENT),
                output_coding_format=hci.CodingFormat(hci.CodecID.TRANSPARENT),
                input_bandwidth=8000, output_bandwidth=8000,
                input_coded_data_size=16, output_coded_data_size=16,
                input_transport_unit_size=1, output_transport_unit_size=1,
            )
        else:
            parameters = hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.ESCO_CVSD_S4]
        if self.device.host.supports_command(hci.HCI_ENHANCED_ACCEPT_SYNCHRONOUS_CONNECTION_REQUEST_COMMAND):
            command = hci.HCI_Enhanced_Accept_Synchronous_Connection_Request_Command(
                bd_addr=connection.peer_address, **parameters.asdict()
            )
        else:
            command = hci.HCI_Accept_Synchronous_Connection_Request_Command(
                bd_addr=connection.peer_address,
                transmit_bandwidth=parameters.transmit_bandwidth,
                receive_bandwidth=parameters.receive_bandwidth,
                max_latency=parameters.max_latency,
                voice_setting=int(hci.VoiceSetting(
                    input_sample_size=hci.VoiceSetting.InputSampleSize.SIZE_16_BITS,
                    input_data_format=hci.VoiceSetting.InputDataFormat.TWOS_COMPLEMENT,
                    air_coding_format=(hci.VoiceSetting.AirCodingFormat.TRANSPARENT_DATA if transparent else hci.VoiceSetting.AirCodingFormat.CVSD),
                )),
                retransmission_effort=parameters.retransmission_effort,
                packet_type=parameters.packet_type,
            )
        await self.device.send_async_command(command)
        self.emit("call", key=key, state=f"正在连接 {codec.name} 语音…")

    def on_link(self, key, protocol, link):
        if self.closed or getattr(protocol, "stopped", False) or self.protocols.get(key, protocol) is not protocol:
            self.spawn(self.disconnect_voice(link))
            return
        if self.router.owner not in (None, key) and self.links:
            self.spawn(self.disconnect_voice(link))
            return
        codec_id = int(selected_codec(protocol))
        compressed = codec_id in (2, 3)
        expected_air = hci.CodecID.TRANSPARENT if compressed else hci.CodecID.CVSD
        if link.air_mode != expected_air:
            self.emit("warning", key=key, message="语音链路格式与协商编码不符，等待重新连接")
            self.spawn(self.disconnect_voice(link))
            return
        try:
            codec = VoiceCodec(codec_id)
        except Exception:
            log.exception("Native voice codec initialization failed")
            self.emit("warning", key=key, message="语音编码器未能启动，请重新连接")
            self.spawn(self.disconnect_voice(link))
            return
        if key in self.links:
            self.audio_closed(key)
        self.router.select(key)
        self.router.configure(48000, adaptive=True)
        self.links[key] = link
        self.voice_codecs[key] = codec
        transmit_buffer = self.voice_buffers[key] = bytearray()
        rate = {1: 8000, 2: 16000, 3: 32000}[codec_id]
        stats = self.statistics[key] = {"codec": {1: "cvsd", 2: "msbc", 3: "lc3_swb"}[codec_id],
            "rate": rate, "pcm_rate": 48000, "rx_packets": 0, "tx_packets": 0,
            "decode_errors": 0, "rx_bytes": 0, "tx_bytes": 0, "decoded_frames": 0}
        traffic = self.traffic[key] = {"rx": TrafficMeter(), "tx": TrafficMeter()}
        payload_ratio = {1: .5, 2: 57 / 60, 3: 58 / 60}[codec_id]
        mix_key = "call:" + key
        self.mixer.add(mix_key)
        self.mixer.call_active, self.mixer.call_source = True, mix_key

        def packet_received(packet):
            if self.links.get(key) is not link:
                return
            data = bytes(packet.data)
            if not data:
                stats["empty_packets"] = stats.get("empty_packets", 0) + 1
                return  # An empty event does not establish an audio duration.
            if len(data) > 255:
                stats["decode_errors"] += 1
                return
            stats["rx_packets"] += 1
            stats["rx_bytes"] += len(data)
            traffic["rx"].add(len(data) * payload_ratio)
            try:
                while len(transmit_buffer) < len(data):
                    transmit_buffer.extend(codec.encode(self.router.read(key, codec.pcm_frame_bytes)))
                uplink = bytes(transmit_buffer[:len(data)])
                del transmit_buffer[:len(data)]
                # Uplink is independent from successful downlink decoding.
                self.device.host.send_sco_sdu(link.handle, uplink)
                stats["tx_packets"] += 1
                stats["tx_bytes"] += len(uplink)
                traffic["tx"].add(len(uplink) * payload_ratio)
                codec.push_rx(data, int(packet.packet_status))
                pcm = codec.read_pcm()
                if pcm:
                    self.mixer.write_voice(mix_key, pcm)
                    stats["decoded_frames"] += len(pcm) // 2
            except Exception:
                stats["decode_errors"] += 1
                log.exception("Native SCO audio packet failed")

        link.sink = packet_received
        link.once("disconnection", lambda reason: self.audio_closed(key, link))
        self.emit("call", key=key, state=f"语音通道已连接 · {codec.name}，正在打开麦克风", phase="active")
        priming = codec.encode(bytes(codec.pcm_frame_bytes))
        self.device.host.send_sco_sdu(link.handle, priming)
        stats["tx_packets"] += 1
        stats["tx_bytes"] += len(priming)
        self.capture_tasks[key] = self.spawn(self.open_microphone(key, link, 48000))
        if self.system_enabled:
            self.system_capture_tasks[key] = self.spawn(self.open_system_capture(key, link, 48000))

        async def check_audio():
            await asyncio.sleep(3)
            while self.links.get(key) is link:
                frames = getattr(self.microphone, "frames", 0)
                stats["microphone_frames"] = frames
                stats["native"] = codec.stats()
                if stats["rx_packets"] == 0:
                    state = "语音已连接，等待设备语音数据"
                elif stats["decoded_frames"] == 0:
                    state = "已收到语音数据，等待有效音频"
                else:
                    state = f"语音通道使用中 · {codec.name} · 麦克风{'工作中' if frames else '等待输入'}"
                self.emit("call", key=key, state=state, phase="active", statistics=dict(stats))
                await asyncio.sleep(5)
        self.spawn(check_audio())

    def retire_microphone(self, microphone):
        task = asyncio.create_task(asyncio.to_thread(microphone.close))
        self.cleanup_tasks.add(task)
        def done(finished):
            self.cleanup_tasks.discard(finished)
            if not finished.cancelled() and finished.exception():
                log.warning("Microphone close failed: %s", finished.exception())
        task.add_done_callback(done)

    def retire_rfcomm_client(self, client):
        async def release():
            channel = getattr(client, "l2cap_channel", None)
            try:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(client.shutdown(), 2)
            finally:
                # Upstream shutdown returns immediately when start failed
                # before creating a multiplexer, even if it owns an L2CAP.
                if channel is not None and getattr(client, "l2cap_channel", None) is channel:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(channel.disconnect(), 1)
        task = asyncio.create_task(release())
        self.cleanup_tasks.add(task)
        task.add_done_callback(self.cleanup_tasks.discard)

    async def disconnect_dlc(self, dlc):
        try:
            await asyncio.wait_for(dlc.disconnect(), 2)
        except (Exception, asyncio.CancelledError) as error:
            # A peer which stops replying must not keep receiver shutdown
            # waiting forever for an RFCOMM UA. Close only its RFCOMM channel.
            channel = dlc.multiplexer.l2cap_channel
            with contextlib.suppress(Exception):
                await asyncio.wait_for(channel.disconnect(), 1)
            if isinstance(error, asyncio.CancelledError):
                raise

    async def disconnect_voice(self, link):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(link.disconnect(), 2)

    async def open_microphone(self, key, link, rate):
        route = CaptureRoute(self.router)
        job = None
        microphone = None
        try:
            async with self.input_lock:
                if self.closed or self.links.get(key) is not link:
                    return
                selected = self.input_device
                log.info("Microphone opening: peer=%s device=%s rate=%s", key, selected, rate)
                job = PendingCapture(lambda: Microphone(route, key, rate, selected, self.emit))
                future = job.start()
                # Shield OS endpoint creation. PendingCapture disposes a late
                # result in its worker even if the call/event loop has ended.
                future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
                await asyncio.wait_for(asyncio.shield(future), MICROPHONE_OPEN_TIMEOUT)
                microphone = job.take()
                if self.closed or self.links.get(key) is not link:
                    return
                self.microphone, self.capture_route = microphone, route
                microphone = None
                route.activate()
                log.info("Microphone active: peer=%s device=%s", key, selected)
                self.emit("microphone", key=key, active=True)
        except Exception as exc:
            log.exception("Microphone open failed: peer=%s", key)
            detail = "打开设备超时，请更换麦克风重试" if isinstance(exc, TimeoutError) else str(exc)
            self.emit("error", message=f"实体麦克风未就绪：{detail}" if self.system_enabled else f"麦克风打不开（上行将静音）：{detail}")
        finally:
            if job:
                late = job.take()
                if late:
                    self.retire_microphone(late)
            if microphone:
                self.retire_microphone(microphone)
            if self.capture_route is not route:
                route.close()
            if self.capture_tasks.get(key) is asyncio.current_task():
                self.capture_tasks.pop(key, None)

    def audio_closed(self, key, link=None):
        if link is not None and self.links.get(key) is not link:
            return
        if key in self.links:
            log.info("SCO disconnected: peer=%s counters=%s", key, self.statistics.get(key))
        old_link = self.links.get(key)
        if self.voice_protection and old_link is not None and key in self.connections:
            self.voice_protection.ended(self.connections[key], old_link)
        retirement = Retirement()
        self.links.pop(key, None)
        self.voice_buffers.pop(key, None)
        codec = self.voice_codecs.pop(key, None)
        if codec:
            retirement.call(codec.close)
        pending = self.capture_tasks.pop(key, None)
        if pending:
            retirement.call(pending.cancel)
        retirement.call(self.mixer.remove, "call:" + key)
        if self.router.owner == key:
            retirement.call(self.close_system_capture)
            if self.capture_route:
                retirement.call(self.capture_route.close)
                self.capture_route = None
            retirement.call(self.router.select, None)
            if self.microphone:
                retirement.call(self.retire_microphone, self.microphone)
                self.microphone = None
            self.mixer.call_active, self.mixer.call_source = False, None
            self.emit("microphone", key=key, active=False)
        retirement.finish()

    async def set_input_device(self, device):
        retry = None
        async with self.input_lock:
            previous = self.input_device
            microphone = self.microphone
            self.input_device = device
            try:
                if microphone:
                    await asyncio.to_thread(microphone.switch_device, device)
            except Exception:
                # A finished call may close its mic during endpoint creation;
                # the selection still applies to the next call in that case.
                if self.microphone is microphone:
                    self.input_device = previous
                    raise
            # Complete the current H2 frame across endpoint hot swaps. Clearing
            # its unsent suffix would corrupt compressed frames on the peer.
            if not self.microphone and not self.closed:
                key = self.router.owner
                link = self.links.get(key)
                if link is not None:
                    rate = 48000
                    retry = self.capture_tasks[key] = self.spawn(self.open_microphone(key, link, rate))
        if retry:
            await asyncio.shield(retry)
            if self.links.get(key) is link and self.microphone is None:
                self.input_device = previous
                raise RuntimeError("新麦克风未能打开，请选择可用的输入设备")

    async def action(self, action, key):
        protocol = self.protocols.get(key)
        if action == "call_audio":
            protocol = await self.ensure_control(key)
        if protocol is None:
            raise ValueError("该设备尚未建立 HFP 语音控制连接")
        if action == "select_call":
            for owner, link in list(self.links.items()):
                if owner != key:
                    await asyncio.wait_for(link.disconnect(), 2)
                    self.audio_closed(owner, link)
            if self.closed or self.protocols.get(key) is not protocol:
                raise ConnectionError("蓝牙语音连接已更换，请重试")
            self.router.select(key)
            self.emit("call", key=key, state="已选为麦克风发送设备")
        elif action == "answer":
            await self.action("select_call", key)
            await protocol.answer_incoming_call()
        elif action == "hangup":
            await protocol.terminate_call()
        elif action == "call_audio":
            await self.action("select_call", key)
            await protocol.setup_audio_connection()

    def close(self):
        if self.closed:
            return
        self.closed = True
        retirement = Retirement()
        retirement.call(self.server.l2cap_server.close)
        if self.device.sdp_service_records.get(0x10002) is self.sdp_record:
            self.device.sdp_service_records.pop(0x10002, None)
        for protocol in list(self.protocols.values()):
            retirement.call(protocol.stop)
        self.protocols.clear()
        self.control_ready.clear()
        self.protocol_tasks.clear()
        for key, client in list(self.clients.items()):
            self.clients.pop(key, None)
            retirement.call(self.retire_rfcomm_client, client)
        retirement.call(self.close_system_capture)
        retirement.call(self.device.remove_listener, "connection", self.on_connection)
        for key in list(self.links):
            retirement.call(self.audio_closed, key)
        if self.capture_route:
            retirement.call(self.capture_route.close)
            self.capture_route = None
        if self.microphone:
            retirement.call(self.retire_microphone, self.microphone)
            self.microphone = None
        retirement.call(self.router.select, None)
        self.links.clear()
        self.voice_buffers.clear()
        for task in list(self.tasks):
            task.cancel()
        retirement.finish()

    async def wait_closed(self):
        while self.tasks or self.cleanup_tasks:
            snapshot = tuple(self.tasks | self.cleanup_tasks)
            await asyncio.gather(*snapshot, return_exceptions=True)
            # gather(done_task) can return synchronously before its scheduled
            # done callback removes it. Prune explicitly and let callbacks run.
            finished = {task for task in snapshot if task.done()}
            self.tasks.difference_update(finished)
            self.cleanup_tasks.difference_update(finished)
            await asyncio.sleep(0)

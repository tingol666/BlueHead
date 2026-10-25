"""HFP hands-free and explicitly selected duplex microphone routing.

Adapted from google/bumble examples/run_hfp_handsfree.py, Apache License 2.0.
Copyright 2021-2026 Google LLC. Modified for per-device isolation and live PCM.
"""
import asyncio
import contextlib
from dataclasses import replace
import logging
import threading

import av
import numpy as np
from bumble import hci, hfp, rfcomm
from bumble.core import PhysicalTransport

from .audio import SwitchableStream, resolve_endpoint
from .audio_runtime import on_audio_thread
from .metrics import TrafficMeter

log = logging.getLogger(__name__)
MICROPHONE_OPEN_TIMEOUT = 5


class CallRouter:
    """Mix mic and OS-filtered computer audio; never read the media mixer."""
    def __init__(self):
        self.owner = None
        self.muted = False
        self.buffer = bytearray()
        self.system_buffer = bytearray()
        self.microphone_gain = self.system_gain = 1.0
        self.device_gains = {}
        self.lock = threading.Lock()
        self.maximum_bytes = 16000
        self.trim_bytes = 8000

    def configure(self, rate):
        with self.lock:
            # Keep at most 90 ms of captured speech, instead of up to 500 ms.
            self.maximum_bytes = int(rate * 2 * .09)
            self.trim_bytes = int(rate * 2 * .06)
            self.buffer.clear()
            self.system_buffer.clear()

    def select(self, key):
        with self.lock:
            self.owner = key
            self.buffer.clear()
            self.system_buffer.clear()

    def clear(self, key):
        with self.lock:
            if self.owner == key:
                self.buffer.clear()

    def write_microphone(self, key, data):
        with self.lock:
            if key == self.owner:
                self.buffer.extend(data)
                if len(self.buffer) > self.maximum_bytes:
                    del self.buffer[:-self.trim_bytes]

    def write_system(self, key, data):
        with self.lock:
            if key == self.owner:
                self.system_buffer.extend(data)
                if len(self.system_buffer) > self.maximum_bytes:
                    del self.system_buffer[:-self.trim_bytes]

    def clear_system(self):
        with self.lock:
            self.system_buffer.clear()

    def read(self, key, length):
        with self.lock:
            if key != self.owner or self.muted:
                self.buffer.clear() if key == self.owner else None
                self.system_buffer.clear() if key == self.owner else None
                return bytes(length)
            take = min(length, len(self.buffer))
            out = bytes(self.buffer[:take]) + bytes(length - take)
            del self.buffer[:take]
            take_system = min(length, len(self.system_buffer))
            system = bytes(self.system_buffer[:take_system]) + bytes(length - take_system)
            del self.system_buffer[:take_system]
            gain = self.device_gains.get(key, 1.0)
            if self.microphone_gain == gain == 1 and not take_system:
                return out
            count = length // 2 * 2
            mic = np.frombuffer(out[:count], "<i2").astype(np.float32) * self.microphone_gain
            computer = np.frombuffer(system[:count], "<i2").astype(np.float32) * self.system_gain
            mixed = (mic + computer) * gain
            peak = float(np.max(np.abs(mixed), initial=0))
            if peak > 32767:
                mixed *= 32767 / peak
            mixed = np.clip(np.rint(mixed), -32768, 32767).astype("<i2")
            return mixed.tobytes() + bytes(length - count)


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


class Msbc:
    """HFP H2 framing, 120 mono samples per 7.5 ms frame."""
    SEQUENCES = (0x08, 0x38, 0xC8, 0xF8)

    def __init__(self):
        self.decoder = av.CodecContext.create("sbc", "r")
        self.decoder.sample_rate = 16000
        self.decoder.layout = "mono"
        self.decoder.open()
        self.encoder = av.CodecContext.create("sbc", "w")
        self.encoder.sample_rate = 16000
        self.encoder.layout = "mono"
        self.encoder.format = "s16"
        self.encoder.options = {"msbc": "1"}
        self.encoder.open()
        if self.encoder.frame_size != 120 or self.encoder.options:
            raise RuntimeError("FFmpeg mSBC encoder unavailable")
        self.receive_buffer = bytearray()
        self.sequence = 0

    def decode(self, data):
        self.receive_buffer.extend(data)
        if len(self.receive_buffer) > 512:
            del self.receive_buffer[:-120]
        result = []
        while len(self.receive_buffer) >= 60:
            b = self.receive_buffer
            if b[0] != 1 or b[1] not in self.SEQUENCES or b[2] != 0xAD:
                del b[0]
                continue
            packet = bytes(b[2:59])
            del b[:60]
            for frame in self.decoder.decode(av.Packet(packet)):
                result.append(frame.to_ndarray().reshape(-1).astype(np.float32) / 32768.0)
        return result

    def encode(self, pcm):
        samples = np.frombuffer(pcm, dtype="<i2").reshape(1, 120)
        frame = av.AudioFrame.from_ndarray(samples, "s16", "mono")
        frame.sample_rate = 16000
        packets = self.encoder.encode(frame)
        if len(packets) != 1 or packets[0].size != 57:
            raise RuntimeError("Invalid mSBC frame length")
        header = bytes([1, self.SEQUENCES[self.sequence]])
        self.sequence = (self.sequence + 1) % 4
        return header + bytes(packets[0]) + b"\x00"


class Microphone(SwitchableStream):
    @on_audio_thread
    def __init__(self, router, key, rate, device, emit):
        import sounddevice as sd
        from .realtime import MicrophoneStream
        self.router, self.key, self.emit = router, key, emit

        def create(selected):
            resolved = resolve_endpoint(selected, "input")
            capture_rate = 48000
            channels = 1
            try:
                sd.check_input_settings(device=resolved, samplerate=capture_rate, channels=1, dtype="float32")
            except sd.PortAudioError:
                info = sd.query_devices(resolved, "input")
                capture_rate = int(info["default_samplerate"])
                try:
                    sd.check_input_settings(device=resolved, samplerate=capture_rate, channels=1, dtype="float32")
                except sd.PortAudioError:
                    channels = min(2, info["max_input_channels"])
            return MicrophoneStream(capture_rate, channels, rate, resolved,
                                    lambda data: self.router.write_microphone(self.key, data), emit)

        super().__init__(create, device, "input")
        try:
            self.start()
        except Exception:
            self.close()
            raise

    def switch_device(self, device):
        super().switch_device(device)
        self.router.clear(self.key)

    @property
    def frames(self):
        return self.stream.snapshot().get("input_frames", 0)

    @property
    def overflows(self):
        return self.stream.snapshot().get("status_events", 0)


class CallManager:
    def __init__(self, device, mixer, emit, input_device=None):
        self.device, self.mixer, self.emit = device, mixer, emit
        self.input_device = input_device
        self.router = CallRouter()
        self.protocols, self.links, self.tasks = {}, {}, set()
        self.connections, self.clients, self.control_ready, self.control_locks = {}, {}, {}, {}
        self.closed = False
        self.microphone = None
        self.capture_route = None
        self.capture_tasks = {}
        self.cleanup_tasks = set()
        self.input_lock = asyncio.Lock()
        self.statistics = {}
        self.traffic = {}
        self.voice_buffers = {}
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
            supported_audio_codecs=[hfp.AudioCodec.CVSD, hfp.AudioCodec.MSBC],
        )
        self.server = rfcomm.Server(device)
        channel = self.server.listen(self.on_dlc)
        device.sdp_service_records[0x10002] = hfp.make_hf_sdp_records(0x10002, channel, self.configuration)
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
        if key not in self.links:
            return {}
        stats = self.statistics.get(key, {})
        traffic = self.traffic.get(key, {})
        system_capture = self.system_capture
        return {"active": True, "codec": stats.get("codec", ""), "rate": stats.get("rate", 0),
                "rx_bitrate": traffic["rx"].snapshot()["bitrate"] if traffic else 0,
                "tx_bitrate": traffic["tx"].snapshot()["bitrate"] if traffic else 0,
                "rx_packets": stats.get("rx_packets", 0), "decode_errors": stats.get("decode_errors", 0),
                "microphone_active": self.microphone is not None,
                "system_audio_active": system_capture is not None and system_capture.thread.is_alive()}

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

    async def set_system_audio(self, enabled):
        self.system_enabled = enabled
        self.close_system_capture()
        key = self.router.owner
        link = self.links.get(key)
        if enabled and link:
            task = self.system_capture_tasks[key] = self.spawn(self.open_system_capture(key, link, self.statistics[key]["rate"]))
            await asyncio.shield(task)
            if self.links.get(key) is link and self.system_capture is None:
                self.system_enabled = False
                raise OSError("电脑声音采集未就绪，已保留麦克风模式")

    def on_connection(self, connection):
        if connection.transport != PhysicalTransport.BR_EDR:
            return
        key = str(connection.peer_address)
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
            self.connections.pop(key, None)
            self.clients.pop(key, None)
        connection.once("disconnection", disconnected)

    async def ensure_control(self, key):
        lock = self.control_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key not in self.protocols:
                connection = self.connections.get(key)
                if connection is None:
                    raise ValueError("移动设备已经断开")
                self.emit("call", key=key, state="正在连接蓝牙语音服务…")
                async with asyncio.timeout(12):
                    record = await hfp.find_ag_sdp_record(connection)
                    if key not in self.protocols:
                        if record is None:
                            raise ValueError("移动设备没有提供 HFP 网关服务")
                        client = rfcomm.Client(connection)
                        mux = await client.start()
                        if key in self.protocols:
                            await client.shutdown()
                        else:
                            dlc = await mux.open_dlc(record[0])
                            self.clients[key] = client
                            self.on_dlc(dlc)
            protocol = self.protocols.get(key)
            if protocol is None:
                raise RuntimeError("蓝牙语音服务未连接")
            ready = self.control_ready[key]
            await asyncio.wait_for(ready.wait(), 12)
            if not protocol._slc_initialized:
                raise RuntimeError("蓝牙语音服务协商未完成")
            return protocol

    def on_dlc(self, dlc):
        connection = dlc.multiplexer.l2cap_channel.connection
        key = str(connection.peer_address)
        self.connections[key] = connection
        if key in self.protocols:
            self.spawn(dlc.disconnect())
            return
        protocol = hfp.HfProtocol(dlc, self.configuration)
        self.protocols[key] = protocol
        ready = self.control_ready[key] = asyncio.Event()
        async def run_protocol():
            try:
                await asyncio.wait_for(protocol.initiate_slc(), 12)
                ready.set()
                self.emit("call", key=key, state="蓝牙语音服务已就绪")
                log.info("HFP service ready: %s", key)
                await protocol.run()
            finally:
                ready.set()
                # Bumble's run() can return after a protocol error. Never leave
                # a dead session registered as a usable headset connection.
                with contextlib.suppress(Exception):
                    await dlc.disconnect()
        self.spawn(run_protocol())
        def request(link_type):
            self.spawn(self.accept_audio(key, protocol, connection, link_type))
        def linked(link):
            self.on_link(key, protocol, link)
        def failed(status):
            log.warning("SCO connection failed: peer=%s status=%s", key, status)
            self.emit("call", key=key, state=f"语音连接失败 · HCI 0x{status:02X}")
        connection.on("sco_request", request)
        connection.on("sco_connection", linked)
        connection.on("sco_connection_failure", failed)
        protocol.on("codec_negotiation", lambda codec: log.info("HFP codec: peer=%s codec=%s", key, codec.name))
        protocol.on("ag_indicator", lambda indicator: self.indicator_changed(key, protocol, indicator))
        protocol.on("ring", lambda: self.emit("call", key=key, state="来电", phase="incoming"))
        def closed():
            connection.remove_listener("sco_request", request)
            connection.remove_listener("sco_connection", linked)
            connection.remove_listener("sco_connection_failure", failed)
            if self.protocols.get(key) is protocol:
                self.protocols.pop(key, None)
                self.control_ready.pop(key, None)
            self.audio_closed(key)
            self.emit("call", key=key, state="蓝牙语音服务已断开，可重连")
        dlc.multiplexer.l2cap_channel.once("close", closed)
        self.emit("call", key=key, state="正在建立蓝牙语音服务…")
        log.info("HFP RFCOMM connected: %s", key)

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
        if self.router.owner and self.router.owner != key and self.links:
            await self.device.send_async_command(hci.HCI_Reject_Synchronous_Connection_Request_Command(
                bd_addr=connection.peer_address, reason=hci.HCI_ErrorCode.CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES_ERROR,
            ))
            self.emit("warning", key=key, message="另一设备正在使用语音通道，请先切换发送设备")
            return
        msbc = protocol.active_codec == hfp.AudioCodec.MSBC
        log.info("SCO request: peer=%s link_type=%s codec=%s", key, link_type, protocol.active_codec)
        if link_type == hci.HCI_Connection_Complete_Event.LinkType.SCO:
            parameters = hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.SCO_CVSD_D1]
            msbc = False
        elif msbc:
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
                    air_coding_format=(hci.VoiceSetting.AirCodingFormat.TRANSPARENT_DATA if msbc else hci.VoiceSetting.AirCodingFormat.CVSD),
                )),
                retransmission_effort=parameters.retransmission_effort,
                packet_type=parameters.packet_type,
            )
        await self.device.send_async_command(command)
        self.emit("call", key=key, state="正在连接宽带语音…" if msbc else "正在连接语音…")

    def on_link(self, key, protocol, link):
        if self.closed:
            return
        if self.router.owner not in (None, key) and self.links:
            self.spawn(link.disconnect())
            return
        if key in self.links:
            self.audio_closed(key)
        self.router.select(key)
        self.links[key] = link
        transparent = link.air_mode == hci.CodecID.TRANSPARENT
        rate = 16000 if transparent else 8000
        self.router.configure(rate)
        codec = Msbc() if transparent else None
        transmit_buffer = bytearray()
        self.voice_buffers[key] = transmit_buffer
        stats = self.statistics[key] = {"codec": "msbc" if codec else "cvsd", "rate": rate,
                                       "rx_packets": 0, "tx_packets": 0, "decode_errors": 0,
                                       "rx_bytes": 0, "tx_bytes": 0, "decoded_frames": 0}
        traffic = self.traffic[key] = {"rx": TrafficMeter(), "tx": TrafficMeter()}
        payload_ratio = 57 / 60 if codec else .5  # H2 transport -> mSBC / PCM host samples -> CVSD air payload.
        log.info("SCO connected: peer=%s air_mode=%s codec=%s", key, link.air_mode, stats["codec"])
        mix_key = "call:" + key
        self.mixer.add(mix_key)
        self.mixer.call_active, self.mixer.call_source = True, mix_key
        log.info("SCO mixer ready: peer=%s", key)
        resampler = av.AudioResampler(format="flt", layout="stereo", rate=self.mixer.rate)

        def packet_received(packet):
            if self.links.get(key) is not link:
                return
            stats["rx_packets"] += 1
            stats["rx_bytes"] += len(packet.data)
            traffic["rx"].add(len(packet.data) * payload_ratio)
            try:
                if codec:
                    while len(transmit_buffer) < len(packet.data):
                        transmit_buffer.extend(codec.encode(self.router.read(key, 240)))
                    uplink = bytes(transmit_buffer[:len(packet.data)])
                    del transmit_buffer[:len(packet.data)]
                else:
                    data = packet.data[:len(packet.data) // 2 * 2]
                    uplink = self.router.read(key, len(data))
                # Keep uplink moving even when a downlink frame is corrupt.
                self.device.host.send_sco_sdu(link.handle, uplink)
                stats["tx_packets"] += 1
                stats["tx_bytes"] += len(uplink)
                traffic["tx"].add(len(uplink) * payload_ratio)
                if codec:
                    decoded = codec.decode(packet.data) if packet.packet_status == 0 else []
                else:
                    decoded = ([np.frombuffer(data, "<i2").astype(np.float32) / 32768.0]
                               if packet.packet_status == 0 else [])
                for samples in decoded:
                    stats["decoded_frames"] += len(samples)
                    frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), "flt", "mono")
                    frame.sample_rate = rate
                    for out in resampler.resample(frame):
                        self.mixer.write(mix_key, out.to_ndarray().reshape(-1, 2).copy())
            except Exception:
                stats["decode_errors"] += 1
                log.exception("SCO audio packet failed")
        link.sink = packet_received
        link.once("disconnection", lambda reason: self.audio_closed(key, link))
        log.info("SCO receive handler ready: peer=%s", key)
        self.emit("call", key=key, state=f"语音通道已连接 · {'mSBC' if codec else 'CVSD'}，正在打开麦克风", phase="active")
        # Start USB OUT even before the controller delivers its first downlink.
        priming = codec.encode(bytes(240)) if codec else bytes(48)
        self.device.host.send_sco_sdu(link.handle, priming)
        stats["tx_packets"] += 1
        stats["tx_bytes"] += len(priming)
        self.capture_tasks[key] = self.spawn(self.open_microphone(key, link, rate))
        if self.system_enabled:
            self.system_capture_tasks[key] = self.spawn(self.open_system_capture(key, link, rate))

        async def check_audio():
            await asyncio.sleep(3)
            while self.links.get(key) is link:
                frames = getattr(self.microphone, "frames", 0)
                stats["microphone_frames"] = frames
                if stats["rx_packets"] == 0:
                    state = "语音已连接，等待移动设备语音数据"
                elif stats["decoded_frames"] == 0:
                    state = "已收到语音数据，解码异常；请查看日志"
                else:
                    state = f"语音通道使用中 · {'宽带语音（mSBC）' if codec else '标准语音（CVSD）'} · 麦克风{'工作中' if frames else '等待输入'}"
                self.emit("call", key=key, state=state, phase="active", statistics=dict(stats))
                log.info("SCO counters: peer=%s %s", key, stats)
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
            log.info("SCO disconnected: peer=%s counters=%s microphone_frames=%s", key,
                     self.statistics.get(key), getattr(self.microphone, "frames", 0))
        self.links.pop(key, None)
        self.voice_buffers.pop(key, None)
        pending = self.capture_tasks.pop(key, None)
        if pending:
            pending.cancel()
        self.mixer.remove("call:" + key)
        if self.router.owner == key:
            self.close_system_capture()
            if self.capture_route:
                self.capture_route.close()
                self.capture_route = None
            self.router.select(None)
            if self.microphone:
                self.retire_microphone(self.microphone)
                self.microphone = None
            self.mixer.call_active, self.mixer.call_source = False, None
            self.emit("microphone", key=key, active=False)

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
            for pending in self.voice_buffers.values():
                pending.clear()
            if not self.microphone and not self.closed:
                key = self.router.owner
                link = self.links.get(key)
                if link is not None:
                    rate = self.statistics[key]["rate"]
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
                    await link.disconnect()
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
        self.close_system_capture()
        self.device.remove_listener("connection", self.on_connection)
        for key in list(self.links):
            self.audio_closed(key)
        if self.capture_route:
            self.capture_route.close()
            self.capture_route = None
        if self.microphone:
            self.retire_microphone(self.microphone)
            self.microphone = None
        self.router.select(None)
        self.links.clear()
        self.voice_buffers.clear()
        for task in list(self.tasks):
            task.cancel()

    async def wait_closed(self):
        while self.tasks or self.cleanup_tasks:
            await asyncio.gather(*self.tasks, *self.cleanup_tasks, return_exceptions=True)

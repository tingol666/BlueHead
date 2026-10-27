"""Multipoint A2DP receiver, reusing Bumble's HCI/L2CAP/AVDTP implementation.

Derived in part from google/bumble examples/run_a2dp_sink.py and speaker.py.
Copyright 2021-2026 Google LLC. Apache License 2.0. Changes: BlueHead contributors.
"""
import asyncio
import contextlib
from dataclasses import dataclass, field
import logging
from pathlib import Path
import queue
import sys
import threading
import time

from bumble import avdtp
from bumble.a2dp import make_audio_sink_service_sdp_records
from bumble.device import Device, DeviceConfiguration
from bumble.pairing import PairingConfig
from .usb_transport import open_transport, load_libusb

from .audio import Decoder, Mixer, AudioOutput
from .adapter import AdapterLease
from .diagnostics import LoopWatchdog
from .protocol import capabilities, negotiated
from .metrics import TrafficMeter
from .continuity import ContinuityGuard
from .errors import Retirement

log = logging.getLogger(__name__)


@dataclass
class Settings:
    mode: str = "advanced"
    transport: str = ""
    codec: str = "auto"
    media_mode: str = "mix"
    name: str = "BlueHead PC"
    buffer_ms: int = 60
    call_buffer_ms: int = 30
    maximum_devices: int = 3
    output_device: int | None = None
    input_device: int | None = None
    output_endpoint_id: str | None = None
    input_endpoint_id: str | None = None
    calls: bool = True
    ldac_enabled: bool = False
    microphone_muted: bool = False
    playback_gain: float = 1.0
    microphone_gain: float = 1.0
    system_gain: float = 1.0
    system_audio_enabled: bool = False
    device_volumes: dict = field(default_factory=dict)


class MediaSession:
    """Decode off the HCI event loop; cap both compressed and PCM backlogs."""
    def __init__(self, key, mixer, emit, has_competitor=lambda: False):
        self.key, self.mixer, self.emit = key, mixer, emit
        self.queue = queue.Queue(maxsize=32)
        self.closed = False
        self.state_lock = threading.RLock()
        self.generation = 0
        self.paused = False
        self.errors = self.drops = 0
        self.packets = self.decoded_frames = 0
        self.format = None
        self.last_error = 0.0
        self.last_decoded = 0.0
        self.traffic = TrafficMeter()
        self.recoveries = 0
        self.codec_errors = self.lost_packets = 0
        self.last_packet = self.started_at = self.paused_at = 0.0
        self.allowed = True
        self.guard = ContinuityGuard(mixer.rate)
        self.has_competitor = has_competitor
        self.worker = threading.Thread(target=self.run, daemon=True, name="media-decoder")
        self.mixer.add(key)
        self.worker.start()

    def configure(self, fmt, prepared=None):
        with self.state_lock:
            self.generation += 1
            self.paused = False
            self.format = fmt
            self.guard.reset()
            self.mixer.set_enabled(self.key, self.allowed)
            self._empty()
            self.mixer.clear(self.key)
            self.queue.put_nowait((fmt, prepared))
        self.emit("format", key=self.key, codec=fmt.codec, rate=fmt.rate, channels=fmt.channels)

    def _empty(self):
        while True:
            try:
                item = self.queue.get_nowait()
                if isinstance(item, tuple) and not isinstance(item[0], (int, str)) and item[1]:
                    item[1].close()
            except queue.Empty:
                break

    def push(self, data):
        with self.state_lock:
            if self.closed or self.paused or self.format is None or not 1 <= len(data) <= 65535:
                return
            self.packets += 1
            self.last_packet = time.monotonic()
            try:
                self.queue.put_nowait((self.generation, bytes(data)))
            except queue.Full:
                # Drop the stale backlog as one generation; reinitialize stateful codecs.
                self.drops += 1
                self.configure(self.format)

    def run(self):
        decoder = None
        try:
            while not self.closed:
                item = self.queue.get()
                if item is None:
                    break
                if item[0] == "resume":
                    if decoder and getattr(decoder, "fmt", None) == item[1] and hasattr(decoder, "restart"):
                        decoder.restart()
                    else:
                        if decoder:
                            decoder.close()
                        decoder = Decoder(item[1], self.mixer.rate)
                    continue
                if not isinstance(item[0], int):
                    if decoder:
                        decoder.close()
                    fmt, prepared = item
                    decoder = prepared or Decoder(fmt, self.mixer.rate)
                    continue
                if decoder is None:
                    continue
                generation, encoded = item
                before = getattr(decoder, "coded_bytes", 0)
                recoveries = getattr(decoder, "recoveries", 0)
                codec_errors = getattr(decoder, "errors", 0)
                lost = getattr(getattr(decoder, "depacketizer", None), "lost", 0)
                decoded_before = self.decoded_frames
                try:
                    if hasattr(decoder, "decode_packet"):
                        decoder.decode_packet(encoded)
                        with self.state_lock:
                            if not self.closed and not self.paused and generation == self.generation:
                                frames = decoder.write_to(self.mixer, self.key)
                                self.decoded_frames += frames
                                if frames:
                                    self.last_decoded = time.monotonic()
                            else:
                                decoder.discard_pcm()
                    else:
                        # Test adapters and isolated experimental codecs share
                        # the same generation check before publishing PCM.
                        for samples in decoder.feed(encoded):
                            with self.state_lock:
                                if not self.closed and not self.paused and generation == self.generation:
                                    self.mixer.write(self.key, samples)
                                    self.decoded_frames += len(samples)
                                    self.last_decoded = time.monotonic()
                except Exception:
                    if hasattr(decoder, "discard_pcm"):
                        decoder.discard_pcm()
                    self.errors += 1
                    if time.monotonic() - self.last_error > 5:
                        self.last_error = time.monotonic()
                        self.emit("warning", message="媒体数据暂不可解码，正在等待有效音频", key=self.key)
                    log.debug("packet decode failed", exc_info=True)
                finally:
                    self.traffic.add(max(0, getattr(decoder, "coded_bytes", before) - before))
                    self.recoveries += getattr(decoder, "recoveries", recoveries) - recoveries
                    errors = getattr(decoder, "errors", codec_errors) - codec_errors
                    self.codec_errors += errors
                    missing = max(0, getattr(getattr(decoder, "depacketizer", None), "lost", 0) - lost)
                    self.lost_packets += missing
                    with self.state_lock:
                        if not self.closed and not self.paused and generation == self.generation:
                            protected = self.guard.observe(self.decoded_frames - decoded_before, 1, missing,
                                                           errors, self.allowed and self.has_competitor())
                            self.mixer.set_enabled(self.key, self.allowed and not protected)
                if (self.packets > 10 and time.monotonic() - max(self.last_decoded, self.last_error) > 5 and
                        getattr(decoder, "synchronizing", False)):
                    self.last_error = time.monotonic()
                    self.emit("warning", message="媒体同步尚未恢复，正在重新寻找有效同步点", key=self.key)
        except Exception as exc:
            log.exception("decoder worker failed")
            self.emit("error", message=f"解码器停止：{exc}", key=self.key)
        finally:
            if decoder:
                decoder.close()

    def set_allowed(self, allowed):
        with self.state_lock:
            self.allowed = allowed
            if not allowed:
                self.guard.reset()
            self.mixer.set_enabled(self.key, allowed and not self.guard.protected)

    def pause(self):
        with self.state_lock:
            if self.closed:
                return
            self.paused = True
            self.paused_at = time.monotonic()
            self.guard.reset()
            self.mixer.set_enabled(self.key, self.allowed)
            self.generation += 1
            self._empty()
            self.mixer.clear(self.key)

    def resume(self):
        with self.state_lock:
            if self.closed:
                return
            self.generation += 1
            self.paused = False
            self.started_at = time.monotonic()
            self.guard.reset()
            self.mixer.set_enabled(self.key, self.allowed)
            self._empty()
            if self.format:
                self.queue.put_nowait(("resume", self.format))

    def close(self):
        if self.closed:
            return
        with self.state_lock:
            self.closed = True
            self.generation += 1
        self._empty()
        self.queue.put_nowait(None)
        self.worker.join(timeout=1)
        self.mixer.remove(self.key)

    def snapshot(self):
        stream = self.mixer.inputs.get(self.key)
        return {"codec": self.format.codec if self.format else "", "rate": self.format.rate if self.format else 0,
                **self.traffic.snapshot(), "playing": not self.paused and time.monotonic() - self.last_decoded < 1.5,
                "buffer_ms": round(stream.frames * 1000 / self.mixer.rate, 1) if stream else 0,
                "protected": self.guard.protected, "selected": self.allowed,
                "lost_packets": self.lost_packets,
                "recoveries": self.recoveries, "decode_errors": self.errors, "sync_errors": self.codec_errors, "dropped_packets": self.drops}


class Receiver:
    def __init__(self, settings, directory: Path, emit, mixer=None):
        self.settings, self.directory, self.emit = settings, directory, emit
        from .native_control import current, RemoteMixer
        client = current() if mixer is None and settings.mode == "advanced" else None
        self.mixer = mixer or (RemoteMixer(client, settings.buffer_ms, settings.call_buffer_ms) if client else
                               Mixer(buffer_ms=settings.buffer_ms, call_buffer_ms=settings.call_buffer_ms))
        self.sessions = {}
        self.connections = {}
        self.media_sinks = {}
        self.media_owner = None
        self.policy_suspended = set()
        self.media_waiting = {}
        self.media_watch_task = None
        self.media_idle_timeout = .8
        self.media_pause_delay = .1
        self.last_voice_at = float("-inf")
        self.media_policy_lock = asyncio.Lock()
        self.device = None
        self.stop_event = asyncio.Event()
        self.output = None
        self.calls = None
        self.avrcp = None
        self.standard_volumes = {}
        self.native = None
        self.audio_lock = asyncio.Lock()
        self.control_tasks = set()
        self.mixer.master = settings.playback_gain
        for key, gains in settings.device_volumes.items():
            self.mixer.set_gain(key, gains.get("down", 1))
            self.mixer.set_gain("call:" + key, gains.get("down", 1))

    def attach(self, device):
        self.device = device
        from .discovery import inquiry_response, add_server_record
        device.inquiry_response = inquiry_response(self.settings.name, self.settings.calls)
        device.sdp_service_records = {
            0x10001: make_audio_sink_service_sdp_records(0x10001)
        }
        from .avrcp import AvrcpManager
        self.avrcp = AvrcpManager(self.emit, self.set_standard_volume,
                                  lambda key: self.standard_volumes.get(key, 1.0))
        self.avrcp.attach(device)
        device.on("connection", self.on_connection)
        listener = avdtp.Listener.for_device(device)
        listener.on("connection", self.on_protocol)
        self.listener = listener
        if self.settings.calls:
            from .calls import CallManager
            if getattr(self.mixer, "remote", False):
                from .native_calls import NativeCallManager
                CallManager = NativeCallManager
            self.calls = CallManager(device, self.mixer, self.emit, self.settings.input_device)
            self.calls.router.muted = self.settings.microphone_muted
            self.calls.router.microphone_gain = self.settings.microphone_gain
            self.calls.router.system_gain = self.settings.system_gain
            self.calls.router.device_gains = {key: gains.get("up", 1) for key, gains in self.settings.device_volumes.items()}
            self.calls.downlink_gains = {key: gains.get("down", 1) for key, gains in self.settings.device_volumes.items()}
            self.calls.system_enabled = self.settings.system_audio_enabled
        add_server_record(device)

    def spawn_control(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.control_tasks.add(task)
        def finished(result):
            self.control_tasks.discard(result)
            if not result.cancelled() and result.exception() and not self.stop_event.is_set():
                log.warning("Bluetooth control operation did not complete", exc_info=result.exception())
        task.add_done_callback(finished)
        return task

    def set_device_gain(self, key, direction, gain):
        if direction == "down":
            self.mixer.set_gain(key, gain * self.standard_volumes.get(key, 1.0))
            if self.calls:
                self.calls.set_downlink_gain(key, gain)
            else:
                self.mixer.set_gain("call:" + key, gain)
        elif self.calls:
            self.calls.router.device_gains[key] = gain

    def set_standard_volume(self, key, volume):
        self.standard_volumes[key] = volume
        gain = self.settings.device_volumes.get(key, {}).get("down", 1.0)
        self.mixer.set_gain(key, gain * volume)

    def set_master_gain(self, bus, gain):
        if bus == "playback":
            self.mixer.master = gain
        elif self.calls:
            setattr(self.calls.router, bus + "_gain", gain)

    def media_codecs(self):
        # Offer usable high-quality endpoints first. The source chooses its
        # supported codec; lower-bandwidth profiles remain explicit choices.
        codec = self.settings.codec
        if codec == "auto":
            return (["ldac"] if self.settings.ldac_enabled else []) + ["aptx_hd", "aptx", "aac", "sbc"]
        if codec == "ldac" and not self.settings.ldac_enabled:
            raise ValueError("请先启用 LDAC 实验解码器")
        return [codec] + ([] if codec == "sbc" else ["sbc"])

    def competing_with(self, key):
        now = time.monotonic()
        return self.settings.media_mode == "mix" and any(
            k != key and not s.paused and not s.closed and now - s.last_packet < 1
            for k, s in list(self.sessions.items()))

    def update_media_gates(self):
        for key, session in list(self.sessions.items()):
            session.set_allowed(self.settings.media_mode == "mix" or key == self.media_owner)

    def voice_in_use(self):
        return self.mixer.call_active or bool(self.calls and self.calls.links)

    def media_owner_idle(self, now=None):
        now = time.monotonic() if now is None else now
        session = self.sessions.get(self.media_owner)
        if session is None or session.closed:
            return True
        if session.paused:
            return now - session.paused_at >= self.media_pause_delay
        return now - max(session.last_packet, session.started_at) >= self.media_idle_timeout

    def emit_media_owner(self):
        self.emit("media_owner", key=self.media_owner or "")

    def ensure_media_watch(self):
        if self.stop_event.is_set():
            return
        if self.media_watch_task is None or self.media_watch_task.done():
            self.media_watch_task = asyncio.create_task(self.watch_media(), name="media-handoff")

    async def watch_media(self):
        try:
            while not self.stop_event.is_set() and self.connections:
                await asyncio.sleep(.1)
                await self.advance_media()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Media handoff monitor stopped")

    def media_started(self, key, sink, session):
        if self.sessions.get(key) is not session or session.closed:
            return
        self.media_sinks[key] = sink
        if self.settings.media_mode == "single":
            if self.media_owner is None and not self.media_policy_lock.locked():
                self.media_owner = key
                self.emit_media_owner()
            elif key != self.media_owner:
                self.media_waiting[key] = time.monotonic()
        self.update_media_gates()
        session.resume()
        self.ensure_media_watch()
        if self.settings.media_mode == "single" and key != self.media_owner:
            self.spawn_control(self.suspend_other_media())

    def media_paused(self, key, session, terminal=False):
        if self.sessions.get(key) is not session or session.closed:
            return
        session.pause()
        # A remote pause/close is user intent. Only app-suspended media stays queued.
        self.media_waiting.pop(key, None)
        self.policy_suspended.discard(key)
        if terminal:
            self.media_sinks.pop(key, None)

    async def suspend_other_media(self):
        async with self.media_policy_lock:
            await self._suspend_other_media()

    async def _suspend_other_media(self):
        for key, sink in list(self.media_sinks.items()):
            if self.stop_event.is_set() or self.settings.media_mode != "single" or key == self.media_owner:
                continue
            stream = sink.stream
            session = self.sessions.get(key)
            if stream and stream.state == avdtp.State.STREAMING:
                if session and not session.paused and time.monotonic() - max(session.last_packet, session.started_at) < self.media_idle_timeout:
                    self.media_waiting.setdefault(key, time.monotonic())
                self.policy_suspended.add(key)
                try:
                    await asyncio.wait_for(stream.stop(), 2)
                    if self.sessions.get(key) is session and session:
                        session.pause()
                except Exception:
                    if self.stop_event.is_set():
                        return
                    log.debug("Source did not accept media suspension", exc_info=True)
                    self.emit("warning", message="设备未响应媒体暂停，请在该设备上手动暂停，或降低媒体码率", key=key)

    async def set_media_mode(self, mode):
        async with self.media_policy_lock:
            self.settings.media_mode = mode
            if mode == "single":
                if self.media_owner not in self.sessions:
                    self.media_owner = next((k for k, session in self.sessions.items() if not session.paused), None)
                self.update_media_gates()
                await self._suspend_other_media()
            else:
                self.update_media_gates()
                for key in list(self.policy_suspended):
                    await self.start_media(key)
                self.policy_suspended.clear()
                self.media_waiting.clear()
            self.emit_media_owner()
        self.emit("media_mode", success=True, mode=mode)
        self.ensure_media_watch()

    async def start_media(self, key):
        if self.stop_event.is_set():
            return False
        sink = self.media_sinks.get(key)
        session = self.sessions.get(key)
        if not sink or not session or session.closed or not sink.stream:
            return False
        if sink.stream.state == avdtp.State.STREAMING:
            if session.paused:
                session.resume()
            return True
        if sink.stream.state == avdtp.State.OPEN:
            try:
                await asyncio.wait_for(sink.stream.start(), 2)
                if self.sessions.get(key) is session and not session.closed:
                    session.resume()
                    return True
            except Exception:
                self.emit("warning", message="请在选定设备上继续播放媒体", key=key)
        return False

    async def _select_media(self, key):
        self.media_owner = key
        self.media_waiting.pop(key, None)
        self.update_media_gates()
        self.emit_media_owner()
        await self._suspend_other_media()
        await self.start_media(key)
        self.policy_suspended.discard(key)

    async def select_media(self, key):
        async with self.media_policy_lock:
            if key not in self.connections or self.stop_event.is_set():
                raise ValueError("设备已断开")
            await self._select_media(key)
        self.ensure_media_watch()

    async def advance_media(self, now=None):
        # Packet silence, not PCM amplitude: silent passages must not steal focus.
        # Never auto-start another media source while a voice link is in use.
        async with self.media_policy_lock:
            now = time.monotonic() if now is None else now
            if self.voice_in_use():
                self.last_voice_at = now
                return
            if (self.settings.media_mode != "single" or self.stop_event.is_set() or
                    now - self.last_voice_at < .7 or not self.media_owner_idle(now)):
                return
            candidates = [key for key in self.media_waiting if key != self.media_owner and key in self.connections
                          and key in self.sessions and not self.sessions[key].closed and key in self.media_sinks]
            if candidates:
                await self._select_media(max(candidates, key=self.media_waiting.get))

    def live_snapshot(self):
        sessions, calls = dict(self.sessions), self.calls
        return [{"key": key, "media": sessions[key].snapshot() if key in sessions else {},
                 "call": calls.snapshot(key) if calls else {},
                 "volumes": self.settings.device_volumes.get(key, {"down": 1.0, "up": 1.0})}
                for key in list(self.connections)]

    def on_connection(self, connection):
        key = str(connection.peer_address)
        current = self.connections.get(key)
        if current is connection:
            return
        if current is not None:
            self.disconnected(key, current)
        if len(self.connections) >= self.settings.maximum_devices:
            self.spawn_control(connection.disconnect())
            self.emit("warning", message="已达到设置的连接数量上限")
            return
        self.connections[key] = connection
        self.emit("device", key=key, state="已连接，等待音频")
        async def read_name():
            try:
                name = await asyncio.wait_for(self.device.request_remote_name(connection.peer_address), 3)
                if self.connections.get(key) is connection:
                    self.emit("name", key=key, name=name)
            except Exception:
                pass
        self.spawn_control(read_name())
        connection.on("disconnection", lambda reason: self.disconnected(key, connection))
        # Keep accepting a second source after the first one pairs.
        self.spawn_control(self.advertise())

    def disconnected(self, key, connection=None):
        if connection is not None and self.connections.get(key) is not connection:
            return
        self.connections.pop(key, None)
        self.media_sinks.pop(key, None)
        self.policy_suspended.discard(key)
        self.media_waiting.pop(key, None)
        if key == self.media_owner:
            self.media_owner = None
            self.update_media_gates()
            self.emit_media_owner()
        session = self.sessions.pop(key, None)
        if session:
            session.close()
        self.standard_volumes.pop(key, None)
        if key in self.settings.device_volumes:
            gain = self.settings.device_volumes[key].get("down", 1.0)
            self.mixer.set_gain(key, gain)
            self.mixer.set_gain("call:" + key, gain)
        else:
            self.mixer.gains.pop(key, None)
            self.mixer.gains.pop("call:" + key, None)
        self.emit("device", key=key, state="已断开")
        self.spawn_control(self.advertise())

    def on_protocol(self, protocol):
        key = str(protocol.l2cap_channel.connection.peer_address)
        if key not in self.connections:
            return
        old = self.sessions.pop(key, None)
        if old:
            old.close()
        factory = MediaSession
        if getattr(self.mixer, "remote", False):
            from .native_control import RemoteMediaSession
            factory = RemoteMediaSession
        session = factory(key, self.mixer, self.emit, lambda: self.competing_with(key))
        self.sessions[key] = session
        self.update_media_gates()
        codecs = self.media_codecs()
        for codec in codecs:
            sink = protocol.add_sink(capabilities(codec))
            original = sink.on_set_configuration_command

            async def configure(config, codec=codec, original=original):
                try:
                    fmt = negotiated(codec, config)
                    # Verify decoder construction before accepting a stream.
                    if getattr(self.mixer, "remote", False):
                        await asyncio.to_thread(session.configure, fmt)
                        check = None
                    else:
                        check = await asyncio.to_thread(Decoder, fmt, self.mixer.rate)
                except Exception:
                    return avdtp.Set_Configuration_Reject(
                        avdtp.AVDTP_MEDIA_CODEC_SERVICE_CATEGORY,
                        avdtp.AVDTP_UNSUPPORTED_CONFIGURATION_ERROR,
                    )
                if session.closed:
                    if check:
                        check.close()
                    return avdtp.Set_Configuration_Reject(
                        avdtp.AVDTP_MEDIA_CODEC_SERVICE_CATEGORY,
                        avdtp.AVDTP_UNSUPPORTED_CONFIGURATION_ERROR,
                    )
                if not getattr(self.mixer, "remote", False):
                    session.configure(fmt, check)
                return await original(config)

            sink.on_set_configuration_command = configure
            reconfigure_original = sink.on_reconfigure_command

            async def reconfigure(config, codec=codec, original=reconfigure_original, endpoint=sink):
                config = list(config)
                try:
                    fmt = negotiated(codec, config)
                    response = await original(config)
                    if response is not None:
                        return response
                    if getattr(self.mixer, "remote", False):
                        await asyncio.to_thread(session.configure, fmt)
                    else:
                        check = await asyncio.to_thread(Decoder, fmt, self.mixer.rate)
                        if session.closed:
                            check.close()
                            raise ValueError("Expired media session")
                        session.configure(fmt, check)
                    if session.closed:
                        raise ValueError("Expired media session")
                except Exception:
                    return avdtp.Reconfigure_Reject(avdtp.AVDTP_MEDIA_CODEC_SERVICE_CATEGORY,
                                                  avdtp.AVDTP_UNSUPPORTED_CONFIGURATION_ERROR)
                endpoint.configuration = [c for c in endpoint.configuration if not isinstance(c, avdtp.MediaCodecCapabilities)] + [c for c in config if isinstance(c, avdtp.MediaCodecCapabilities)]
                return None

            sink.on_reconfigure_command = reconfigure
            # aptX classic uses raw AVDTP media; other formats carry RTP.
            sink.on_avdtp_packet = session.push
            if getattr(self.mixer, "remote", False):
                def bind(sink=sink):
                    if sink.stream and sink.stream.rtp_channel:
                        session.bind(sink.stream.rtp_channel)
                sink.on("rtp_channel_open", bind)
                sink.on("rtp_channel_close", session.unbind)
            sink.on("start", lambda sink=sink: self.media_started(key, sink, session))
            sink.on("suspend", lambda: self.media_paused(key, session))
            sink.on("close", lambda: self.media_paused(key, session, True))
            sink.on("abort", lambda: self.media_paused(key, session, True))
            sink.on("rtp_channel_close", lambda: self.media_paused(key, session, True))
        if getattr(self.mixer, "remote", False):
            protocol.on("close", session.unbind)
        protocol.on("close", lambda: self.media_paused(key, session, True))

    async def advertise(self):
        if self.device and not self.stop_event.is_set():
            await self.device.set_discoverable(True)
            await self.device.set_connectable(True)

    async def initialize_transport(self, device, config, transport, timeout=12):
        """Bound setup independently of the media path and honor Stop mid-reset."""
        async def initialize():
            if hasattr(transport, 'wait_ready'):
                await transport.wait_ready(lambda message: self.emit('status', message=message))
            try:
                await asyncio.wait_for(device.power_on(), timeout)
            except TimeoutError:
                raise TimeoutError('蓝牙适配器启动超时，未收到完整初始化响应；请检查连接和专用接收驱动') from None
            from .discovery import update_inquiry_power
            await update_inquiry_power(device, config.name, self.settings.calls)
            await self.advertise()

        setup = asyncio.create_task(initialize())
        stopped = asyncio.create_task(self.stop_event.wait())
        terminated = asyncio.ensure_future(asyncio.shield(transport.source.terminated))
        try:
            await asyncio.wait([setup, stopped, terminated],
                               timeout=timeout + (65 if hasattr(transport, 'wait_ready') else 0),
                               return_when=asyncio.FIRST_COMPLETED)
            if self.stop_event.is_set():
                return False
            if terminated.done():
                failure = getattr(transport.source, 'failure', None)
                if failure is not None:
                    raise failure
                raise ConnectionError("蓝牙适配器在启动时断开，请检查专用接收驱动或重新插拔适配器")
            if setup.done():
                await setup
                return True
            raise TimeoutError("蓝牙适配器启动超时，未收到完整初始化响应；请重新插拔适配器，内置适配器可重启 Windows 后重试")
        finally:
            for task in (setup, stopped, terminated):
                task.cancel()
            await asyncio.gather(setup, stopped, terminated, return_exceptions=True)

    async def run(self):
        if self.settings.mode == "native":
            from .native import NativeReceiver
            self.native = NativeReceiver(self.emit)
            await self.native.run(self.stop_event)
            return
        if not self.settings.transport.startswith(("usb:", "usb-id:")):
            raise ValueError("请选择一个 USB 蓝牙适配器；程序不会自动更改驱动")
        transport_spec = self.settings.transport
        if self.settings.calls and "+sco=" not in transport_spec:
            # Bumble leaves the USB voice interface disabled unless requested.
            # Auto-selection follows CVSD/mSBC setup and releases its bandwidth
            # again when the call ends.
            transport_spec += "+sco=0"
        self.directory.mkdir(parents=True, exist_ok=True)
        lease = AdapterLease(transport_spec)
        watchdog = LoopWatchdog()
        watchdog.start()
        try:
            if not getattr(self.mixer, "remote", False):
                lease.acquire()
            async with await open_transport(transport_spec) as transport:
                config = DeviceConfiguration()
                config.name = self.settings.name
                config.class_of_device = 0x240404  # audio/video wearable headset
                config.classic_enabled = True
                config.le_enabled = False
                config.keystore = "JsonKeyStore:" + str(self.directory / "pairing.json")
                device = Device.from_config_with_hci(config, transport.source, transport.sink)
                # Numeric comparison is not meaningful on a headset; pair on the phone.
                device.pairing_config_factory = lambda _: PairingConfig(mitm=False)
                self.attach(device)
                output_factory = AudioOutput
                if getattr(self.mixer, "remote", False):
                    from .native_control import RemoteOutput
                    output_factory = RemoteOutput
                self.output = output_factory(self.mixer, self.settings.output_device)
                self.output.start()
                if not await self.initialize_transport(device, config, transport):
                    return
                self.emit("status", message=f"已就绪：在移动设备蓝牙列表连接 {config.name}")
                stopped = asyncio.create_task(self.stop_event.wait())
                terminated = asyncio.ensure_future(asyncio.shield(transport.source.terminated))
                try:
                    await asyncio.wait([stopped, terminated], return_when=asyncio.FIRST_COMPLETED)
                finally:
                    stopped.cancel()
                    terminated.cancel()
                if not self.stop_event.is_set():
                    failure = getattr(transport.source, 'failure', None)
                    if failure is not None:
                        message = ("原生音频后台连接已中断，请重新开始接收" if
                                   getattr(transport.source, 'failure_category', '') == 'native_channel' else str(failure))
                        raise ConnectionError(message) from failure
                    raise ConnectionError("蓝牙适配器已断开")
                if self.avrcp:
                    await self.avrcp.aclose()
                for connection in list(self.connections.values()):
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(connection.disconnect(), 2)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(device.power_off(), 2)
        except Exception as exc:
            if type(exc).__name__ in ("USBErrorNotSupported", "USBErrorAccess", "USBErrorBusy"):
                raise RuntimeError("所选适配器仍被 Windows 蓝牙驱动占用，或尚未关联 WinUSB。高音质模式需要独占接口；可先选原生模式，或按使用说明准备一个适配器。") from exc
            raise
        finally:
            original_error = sys.exc_info()[1]
            retirement = Retirement()
            try:
                if self.media_watch_task:
                    self.media_watch_task.cancel()
                    try:
                        await self.media_watch_task
                    except asyncio.CancelledError:
                        pass  # This is the cancellation we just requested.
                    except Exception as error:
                        retirement.failed(error)
                if self.calls:
                    retirement.call(self.calls.close)
                    await retirement.async_call(self.calls.wait_closed)
                if self.avrcp:
                    await retirement.async_call(self.avrcp.aclose)
                for session in list(self.sessions.values()):
                    retirement.call(session.close)
                self.sessions.clear()
                if self.output:
                    await retirement.async_call(asyncio.to_thread, self.output.close)
            finally:
                self.stop_event.set()
                for task in list(self.control_tasks):
                    task.cancel()
                await asyncio.gather(*self.control_tasks, return_exceptions=True)
                self.control_tasks.clear()
                watchdog.close()
                lease.close()
                self.emit("status", message="已停止")
            retirement.finish(original_error)

    async def set_audio_device(self, direction, device, endpoint_id=None):
        if self.settings.mode == "native":
            raise ValueError("原生接收方式由 Windows 选择声音设备")
        async with self.audio_lock:
            if self.stop_event.is_set():
                raise RuntimeError("接收器正在停止")
            if endpoint_id is not None:
                from .endpoints import find_endpoint
                device = await asyncio.to_thread(find_endpoint, endpoint_id, direction)
            if direction == "output":
                if self.output is None:
                    raise RuntimeError("请等待声音设备启动完成")
                await asyncio.to_thread(self.output.switch_device, device)
                self.settings.output_device = device
            elif direction == "input":
                if self.calls:
                    await self.calls.set_input_device(device)
                self.settings.input_device = device
            else:
                raise ValueError("未知音频方向")
            setattr(self.settings, direction + "_endpoint_id", endpoint_id)
            return device

    async def refresh_audio(self, force=False):
        from .audio_runtime import refresh_devices
        async with self.audio_lock:
            async with contextlib.AsyncExitStack() as stack:
                if self.calls:
                    await stack.enter_async_context(self.calls.input_lock)
                native = bool(getattr(self.mixer, "remote", False))
                # Native hotplug/backoff belongs to its one control owner.
                # An inventory poll must not turn a permission latch into
                # one forced retry per second; only explicit rescan may do so.
                retry_errors = await asyncio.to_thread(self.mixer.client.retry_audio) if native and force else []
                mapped, errors = await asyncio.to_thread(refresh_devices, {
                    "output": self.settings.output_device, "input": self.settings.input_device}, {
                    "output": self.settings.output_endpoint_id, "input": self.settings.input_endpoint_id})
                errors = retry_errors + errors
                old_input = self.calls.input_device if self.calls else None
                if native and self.output:
                    desired = self.settings.output_endpoint_id
                    if desired and mapped['output'] is not None and self.output.endpoint_id != desired:
                        try:
                            await asyncio.to_thread(self.output.switch_device, mapped['output'])
                        except OSError as error:
                            errors.append(str(error))
                    else:
                        self.output.device = mapped['output']
                        self.output.endpoint_id = desired if mapped['output'] is not None else None
                self.settings.output_device = mapped["output"]
                self.settings.input_device = mapped["input"]
                for direction in ("input", "output"):
                    if mapped[direction] is None and not native:
                        setattr(self.settings, direction + "_endpoint_id", None)
                restore_input = bool(native and self.calls and self.settings.input_endpoint_id and
                                     mapped['input'] is not None and mapped['input'] != old_input)
                if self.calls and not restore_input:
                    self.calls.input_device = mapped["input"]
            # set_input_device owns input_lock itself. The outer route lock
            # still prevents a concurrent UI selection during this reconcile.
            if restore_input:
                try:
                    await self.calls.set_input_device(mapped['input'])
                except OSError as error:
                    errors.append(str(error))
            return mapped, errors

    async def action(self, action, key):
        if action in ("play", "pause", "play_pause", "stop", "next", "previous") and self.avrcp:
            await self.avrcp.command(key, action)
        elif action == "media_mode":
            await self.set_media_mode(key)
        elif action == "select_media":
            await self.select_media(key)
        elif action == "disconnect" and key in self.connections:
            await self.connections[key].disconnect()
        elif action in ("answer", "hangup", "call_audio", "select_call") and self.calls:
            await self.calls.action(action, key)


def usb_adapters():
    """Enumerate HCI descriptors. No driver replacement, reset, or interface claim."""
    from .native_control import current
    if (client := current()) is not None:
        return client.request('usb_inventory')
    import usb1
    load_libusb()
    result, counts = [], {}
    with usb1.USBContext() as context:
        for device in context.getDeviceList(skip_on_error=True):
            try:
                is_hci = (device.getDeviceClass(), device.getDeviceSubClass(), device.getDeviceProtocol()) == (0xE0, 1, 1)
                if not is_hci:
                    is_hci = any(
                        (s.getClass(), s.getSubClass(), s.getProtocol()) == (0xE0, 1, 1)
                        for cfg in device for interface in cfg for s in interface
                    )
                if not is_hci:
                    continue
                identity = f"{device.getVendorID():04X}:{device.getProductID():04X}"
                index = counts.get(identity, 0)
                counts[identity] = index + 1
                product_name = "USB 蓝牙"
                result.append({"transport": f"usb:{identity}#{index}", "name": f"{product_name} {identity} · 设备 {index + 1}"})
            except usb1.USBError:
                continue
    return result


class Runner:
    def __init__(self, emit):
        self.emit = emit
        self.receiver = None
        self.loop = None
        self.thread = None
        self.stop_requested = threading.Event()

    def start(self, settings, directory):
        if self.thread and self.thread.is_alive():
            if self.stop_requested.is_set():
                self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError("接收器尚未停止，请稍后重试")
        self.stop_requested.clear()
        self.thread = threading.Thread(target=self._run, args=(settings, directory), daemon=True)
        self.thread.start()

    def _run(self, settings, directory):
        async def run():
            self.loop = asyncio.get_running_loop()
            self.receiver = Receiver(settings, directory, self.emit)
            try:
                if not self.stop_requested.is_set():
                    await self.receiver.run()
            except Exception as exc:
                log.exception("receiver stopped")
                self.emit("error", message=f"启动失败：{exc}")
            finally:
                self.receiver = None
                self.emit("stopped")
        asyncio.run(run())

    def stop(self):
        self.stop_requested.set()
        if self.loop and self.loop.is_running() and self.receiver:
            self.loop.call_soon_threadsafe(self.receiver.stop_event.set)

    def action(self, action, key):
        if self.loop and self.receiver:
            future = asyncio.run_coroutine_threadsafe(self.receiver.action(action, key), self.loop)
            def done(f):
                try:
                    f.result()
                except Exception as exc:
                    if action == "media_mode":
                        self.emit("media_mode", success=False, mode=self.receiver.settings.media_mode if self.receiver else "mix", message=f"切换失败：{exc}")
                    else:
                        self.emit("error", message=f"操作失败：{exc}")
            future.add_done_callback(done)

    def set_system_audio(self, enabled, request_id):
        async def apply():
            try:
                receiver = self.receiver
                if receiver is None or receiver.calls is None:
                    raise OSError("当前接收方式没有启用通话")
                await receiver.calls.set_system_audio(enabled)
                receiver.settings.system_audio_enabled = enabled
                self.emit("system_audio", success=True, enabled=enabled, request_id=request_id)
            except asyncio.CancelledError:
                self.emit("system_audio", success=False, enabled=False, request_id=request_id,
                          message="通话或接收已停止，电脑声音混合未启用")
            except Exception as error:
                self.emit("system_audio", success=False, enabled=False, request_id=request_id, message=str(error))
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(apply(), self.loop)
        else:
            self.emit("system_audio", success=False, enabled=False, request_id=request_id, message="接收器尚未就绪")

    def set_audio_device(self, direction, device, request_id, endpoint_id=None):
        async def apply():
            try:
                receiver = self.receiver
                if receiver is None:
                    raise RuntimeError("接收器已经停止")
                selected = await receiver.set_audio_device(direction, device, endpoint_id)
                self.emit("audio_device", direction=direction, device=selected, endpoint_id=endpoint_id, request_id=request_id, success=True)
            except Exception as exc:
                log.warning("Audio endpoint switch failed", exc_info=True)
                self.emit("audio_device", direction=direction, device=device, request_id=request_id,
                          success=False, message=f"切换失败，已保留原设备：{exc}")
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(apply(), self.loop)
        else:
            self.emit("audio_device", direction=direction, device=device, request_id=request_id,
                      success=False, message="接收器尚未启动")

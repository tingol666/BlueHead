"""Bumble HFP control with all duplex audio owned by the native runtime."""
import asyncio
import contextlib
import threading
import time

from bumble import hci

from .calls import CallManager
from .hfp_compat import selected_codec
from .voice import CODEC_NAMES


class _Gains(dict):
    def __init__(self, values, changed):
        super().__init__(values)
        self.changed = changed

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.changed()

    def pop(self, key, default=None):
        result = super().pop(key, default)
        self.changed()
        return result


class RemoteRouter:
    def __init__(self, client):
        object.__setattr__(self, "client", client)
        object.__setattr__(self, "lock", threading.RLock())
        object.__setattr__(self, "owner", None)
        object.__setattr__(self, "device_gains", _Gains({}, self.sync))
        object.__setattr__(self, "device_standard_gains", _Gains({}, self.sync))
        object.__setattr__(self, "microphone_gain", 1.0)
        object.__setattr__(self, "system_gain", 1.0)
        object.__setattr__(self, "muted", False)

    def __setattr__(self, name, value):
        if name in ("device_gains", "device_standard_gains"):
            value = _Gains(value, self.sync)
        object.__setattr__(self, name, value)
        if name in ("device_gains", "device_standard_gains", "microphone_gain", "system_gain", "muted"):
            self.sync()

    def sync(self):
        self.client.request("router_gain", key=self.owner or "", microphone=self.microphone_gain, system=self.system_gain,
                            device=self.device_gains.get(self.owner, 1.0) * self.device_standard_gains.get(self.owner, 1.0),
                            muted=self.muted)

    def select(self, key):
        self.owner = key
        self.sync()

    def configure(self, rate, *, adaptive=False):
        if rate != 48000 or not adaptive:
            raise ValueError("原生语音链路使用独立时钟匹配的 48 kHz PCM")

    def clear(self, key):
        pass

    def clear_system(self):
        pass

    def snapshot(self):
        result = self.client.request("snapshot")["uplink"]
        return {"adaptive": True, "rate": 48000, "buffer_ms": 30, "buffer_reference": "render_center", **result}

    def close(self):
        pass


class _Endpoint:
    def __init__(self, client, direction, epoch):
        self.client, self.direction, self.epoch = client, direction, epoch
        self.closed = False
        self.thread = self

    @property
    def frames(self):
        return self.client.request("snapshot")["microphone_frames" if self.direction == "input" else "system_frames"]

    @property
    def overflows(self):
        return self.client.request("snapshot")["bridge_errors"]

    def is_alive(self):
        return not self.closed and self.client.request("snapshot").get("system_active" if self.direction == "system" else "microphone_active", False)

    def close(self):
        if not self.closed:
            self.closed = True
            self.client.audio(self.direction, start=False, voice_epoch=self.epoch)


class _Voice:
    def __init__(self, client, key, epoch, codec):
        self.client, self.key, self.epoch, self.codec = client, key, epoch, codec
        self.closed = False

    def close(self):
        if not self.closed:
            self.closed = True
            self.client.request("voice_stop", key=self.key, epoch=self.epoch)

    def stats(self):
        return self.client.request("snapshot")["voice"]


class NativeCallManager(CallManager):
    def __init__(self, device, mixer, emit, input_device=None):
        self.client = mixer.client
        super().__init__(device, mixer, emit, input_device, router=RemoteRouter(self.client))
        self.native_epochs, self.native_meters = {}, {}

    def current_voice(self, key, link, epoch):
        return (not self.closed and self.links.get(key) is link and
                self.native_epochs.get(key) == epoch)

    def on_link(self, key, protocol, link):
        if self.closed or getattr(protocol, "stopped", False) or self.protocols.get(key, protocol) is not protocol:
            self.spawn(self.disconnect_voice(link))
            return
        if self.router.owner not in (None, key) and self.links:
            self.spawn(self.disconnect_voice(link))
            return
        codec = int(selected_codec(protocol))
        expected = hci.CodecID.TRANSPARENT if codec in (2, 3) else hci.CodecID.CVSD
        if link.air_mode != expected:
            self.emit("warning", key=key, message="语音链路格式与协商编码不符，等待重新连接")
            self.spawn(self.disconnect_voice(link))
            return
        if key in self.links:
            self.audio_closed(key)
        self.mixer.add("call:" + key)
        try:
            result = self.client.request("voice_start", key=key, handle=link.handle, codec=codec)
        except OSError:
            self.mixer.remove("call:" + key)
            self.spawn(self.disconnect_voice(link))
            return
        epoch = result["epoch"]
        self.native_epochs[key] = epoch
        self.native_meters[key] = (time.monotonic(), 0, 0)
        self.router.select(key)
        self.links[key] = link
        self.voice_codecs[key] = _Voice(self.client, key, epoch, codec)
        self.statistics[key] = {"codec": {1: "cvsd", 2: "msbc", 3: "lc3_swb"}[codec],
                                "rate": {1: 8000, 2: 16000, 3: 32000}[codec]}
        self.mixer.call_active, self.mixer.call_source = True, "call:" + key
        # Native SCO registration precedes this event-loop callback's return.
        # Any pre-registration packet forwarded by HCI is never decoded here.
        link.sink = lambda _packet: None
        link.once("disconnection", lambda _reason: self.audio_closed(key, link))
        self.emit("call", key=key, state=f"语音通道已连接 · {CODEC_NAMES[codec]}，正在打开麦克风", phase="active")
        self.capture_tasks[key] = self.spawn(self.open_microphone(key, link, 48000))
        if self.system_enabled:
            self.system_capture_tasks[key] = self.spawn(self.open_system_capture(key, link, 48000))

    async def open_microphone(self, key, link, rate):
        from .audio import resolve_endpoint
        epoch = None
        try:
            async with self.input_lock:
                if self.closed or self.links.get(key) is not link:
                    return
                epoch = self.native_epochs[key]
                endpoint = await asyncio.to_thread(resolve_endpoint, self.input_device, "input")
                if not self.current_voice(key, link, epoch):
                    return
                await asyncio.to_thread(self.client.audio, "input", endpoint, voice_epoch=epoch)
                if self.current_voice(key, link, epoch):
                    self.microphone = _Endpoint(self.client, "input", epoch)
                    self.emit("microphone", key=key, active=True)
        except (OSError, TimeoutError) as error:
            if epoch is not None and self.current_voice(key, link, epoch):
                self.emit("error", message=f"实体麦克风未就绪：{error}")
        finally:
            if self.capture_tasks.get(key) is asyncio.current_task():
                self.capture_tasks.pop(key, None)

    async def open_system_capture(self, key, link, rate):
        epoch = None
        try:
            async with self.system_lock:
                if self.closed or not self.system_enabled or self.links.get(key) is not link:
                    return
                epoch = self.native_epochs[key]
                ticket = self.client.request("system_audio", enabled=True, voice_epoch=epoch,
                                             endpoint_id=getattr(self, "system_endpoint", None) or "")
                await asyncio.to_thread(self.client.wait_audio, ticket)
                if self.current_voice(key, link, epoch) and self.system_enabled:
                    self.system_capture = _Endpoint(self.client, "system", epoch)
                    self.emit("system_audio_state", active=True, message="电脑声音已加入发送")
        except (OSError, TimeoutError) as error:
            # A late failure belongs to the request's original epoch. Looking
            # up the *current* epoch here can disable a replacement call.
            if epoch is not None and self.current_voice(key, link, epoch):
                with contextlib.suppress(OSError):
                    self.client.request("system_audio", enabled=False, voice_epoch=epoch)
                self.emit("system_audio_state", active=False, message=f"电脑声音未加入发送，麦克风继续使用：{error}")
        finally:
            if self.system_capture_tasks.get(key) is asyncio.current_task():
                self.system_capture_tasks.pop(key, None)

    async def set_system_source(self, endpoint):
        async with self.system_lock:
            capture = self.system_capture
            if capture is not None and not capture.closed:
                await asyncio.to_thread(self.client.audio, "system", endpoint, voice_epoch=capture.epoch)
            self.system_endpoint = endpoint

    def close_system_capture(self):
        for task in list(self.system_capture_tasks.values()):
            task.cancel()
        self.system_capture_tasks.clear()
        capture, self.system_capture = self.system_capture, None
        self.system_route = None
        epoch = self.native_epochs.get(self.router.owner)
        if epoch is not None:
            with contextlib.suppress(OSError):
                self.client.request("system_audio", enabled=False, voice_epoch=epoch)
        if capture:
            self.retire_microphone(capture)

    async def set_input_device(self, device):
        from .audio import resolve_endpoint
        async with self.input_lock:
            previous = self.input_device
            key = self.router.owner
            link, epoch = self.links.get(key), self.native_epochs.get(key)
            self.input_device = device
            try:
                if link is not None and epoch is not None:
                    endpoint = await asyncio.to_thread(resolve_endpoint, device, "input")
                    if self.current_voice(key, link, epoch):
                        try:
                            await asyncio.to_thread(self.client.audio, "input", endpoint, voice_epoch=epoch)
                        except OSError:
                            # Hangup/replacement cancels the old native prepare;
                            # the valid user selection still belongs to the next
                            # call. A failure in the current route must roll back.
                            if self.current_voice(key, link, epoch):
                                raise
            except (OSError, ValueError, asyncio.CancelledError):
                self.input_device = previous
                raise

    def snapshot(self, key):
        link = self.links.get(key)
        codec_context = self.voice_codecs.get(key)
        epoch = self.native_epochs.get(key)
        statistics = dict(self.statistics.get(key, {}))
        if link is None or codec_context is None or epoch is None:
            return {}
        codec = codec_context.codec
        native = self.client.request("snapshot")
        voice = native["voice"]
        if (self.links.get(key) is not link or self.native_epochs.get(key) != epoch or
                voice.get('epoch', epoch) != epoch):
            return {}
        now = time.monotonic()
        # The first sample establishes a baseline. Lifetime bytes divided by a
        # synthetic 1 ms interval would fabricate a huge bitrate at call start.
        previous, rx, tx = self.native_meters.get(key, (now, voice["rx_bytes"], voice["tx_bytes"]))
        elapsed = max(.001, now - previous)
        ratio = {1: .5, 2: 57 / 60, 3: 58 / 60}[codec]
        self.native_meters[key] = (now, voice["rx_bytes"], voice["tx_bytes"])
        result = {"active": voice["active"], **statistics,
                  "channels": 1, "channel_mode": "mono", "depth": 0, "depth_status": "not_signalled",
                  "rx_bitrate": max(0, int((voice["rx_bytes"] - rx) * 8 * ratio / elapsed)),
                  "tx_bitrate": max(0, int((voice["tx_bytes"] - tx) * 8 * ratio / elapsed)),
                  "rx_packets": voice["rx_packets"], "decode_errors": voice["decode_errors"],
                  "tx_packets": voice["tx_packets"],
                  "empty_packets": 0, "voice": voice, "uplink_clock": {"adaptive": True, **native["uplink"]},
                  "microphone_active": native.get("microphone_active", False),
                  "system_audio_active": native.get("system_active", False)}
        result["system_feedback_blocked"] = native.get("system_feedback_blocked", False)
        return result if self.links.get(key) is link else {}

    def audio_closed(self, key, link=None):
        if link is not None and self.links.get(key) is not link:
            return
        epoch = self.native_epochs.get(key)
        try:
            super().audio_closed(key, link)
        finally:
            if self.links.get(key) is None and self.native_epochs.get(key) == epoch:
                self.native_epochs.pop(key, None)
                self.native_meters.pop(key, None)

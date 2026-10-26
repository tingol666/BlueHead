"""Control-only bridge to the native USB/audio owner. Never transports PCM.

Production uses the private pipe inherited from BlueHead.Runtime.exe. Explicit
owned clients are for offline tests; they do not open USB/audio on construction.
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import threading
import time
import uuid

from . import __version__
from .errors import NativeAudioError, SourceExpired

MAX_REPLY = 1048576
_client = None
_client_lock = threading.Lock()


class NativeClient:
    def __init__(self, pipe=None, *, spawn=False, executable=None, assets=None, offline=True):
        import _winapi
        self.handle = None
        self.process = None
        self.lock = threading.RLock()
        self.closed = False
        self.pipe = pipe or rf"\\.\pipe\BlueHead-RT-{uuid.uuid4().hex}"
        assets = Path(assets or Path(__file__).resolve().parent / "assets").resolve()
        if spawn:
            executable = Path(executable or assets / "BlueHead.Runtime.exe")
            self.process = subprocess.Popen(
                [str(executable), "--rpc", self.pipe, "--assets", str(assets), "--parent-pid", str(os.getpid()),
                 *(["--offline-test"] if offline else [])],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        deadline = time.monotonic() + 5
        try:
            while True:
                try:
                    self.handle = _winapi.CreateFile(self.pipe, 0xC0000000, 0, 0, 3, 0x40000000, 0)
                    break
                except OSError:
                    if time.monotonic() >= deadline or (self.process and self.process.poll() is not None):
                        raise OSError("原生音频后台未能连接") from None
                    time.sleep(.01)
            hello = self.request("hello")
            if hello.get("version") != __version__ or hello.get("abi") != 1:
                raise OSError("原生音频后台版本不匹配，请完整更新程序")
        except BaseException:
            self.close()
            raise

    def _io(self, data=None, length=0, timeout=10):
        import _winapi
        deadline = time.monotonic() + timeout
        if data is not None:
            operation, error = _winapi.WriteFile(self.handle, data, overlapped=True)
        else:
            operation, error = _winapi.ReadFile(self.handle, length, overlapped=True)
        try:
            if error == 997:
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                if _winapi.WaitForMultipleObjects([operation.event], False, remaining) != 0:
                    operation.cancel()
                    operation.GetOverlappedResult(True)
                    raise TimeoutError("原生控制请求超时，音频线程与控制请求独立运行")
            count, error = operation.GetOverlappedResult(True)
            if error or count <= 0:
                raise OSError("原生音频后台连接已关闭")
            return count if data is not None else bytes(operation.getbuffer()[:count])
        finally:
            # GetOverlappedResult above retires every submitted operation before
            # its Python-owned allocation is released.
            pass

    def _read(self, length):
        result = bytearray()
        while len(result) < length:
            result.extend(self._io(length=length - len(result)))
        return bytes(result)

    def request(self, op, **values):
        with self.lock:
            if self.closed or self.handle is None:
                raise OSError("原生音频后台已关闭")
            payload = json.dumps({"op": op, **values}, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(payload) > 131072:
                raise ValueError("原生控制请求过长")
            packet = struct.pack("<I", len(payload)) + payload
            try:
                offset = 0
                while offset < len(packet):
                    offset += self._io(data=packet[offset:])
                length, = struct.unpack("<I", self._read(4))
                if not 0 < length <= MAX_REPLY:
                    raise OSError("原生控制响应无效")
                response = json.loads(self._read(length))
            except (OSError, ValueError):
                # A timed-out/partial exchange cannot safely be reused: a late
                # response could otherwise be mistaken for the next request.
                import _winapi
                _winapi.CloseHandle(self.handle)
                self.handle = None
                raise
            if not response.get("ok"):
                if response.get("category") == "source_expired":
                    raise SourceExpired(response.get("error", "Audio source session has expired"))
                if response.get("category"):
                    raise NativeAudioError(response)
                raise OSError(response.get("error", "原生音频操作失败"))
            return response.get("result")

    def wait_audio(self, ticket, timeout=8):
        deadline = time.monotonic() + timeout
        while True:
            result = self.request("audio_result", ticket=ticket)
            if result.get("done"):
                if not result.get("success"):
                    if result.get("category"):
                        raise NativeAudioError(result)
                    raise OSError(result.get("error", "音频设备操作已取消"))
                return result
            if time.monotonic() >= deadline:
                if not self.request("audio_cancel", ticket=ticket):
                    result = self.request("audio_result", ticket=ticket)
                    if result.get("success"):
                        return result
                raise TimeoutError("音频设备准备超时，已取消本次切换")
            time.sleep(.01)

    def audio(self, direction, endpoint_id=None, start=True, **values):
        ticket = self.request("audio_switch", direction=direction, endpoint_id=endpoint_id or "", start=start, **values)
        return self.wait_audio(ticket)

    def retry_audio(self, timeout=8):
        """Explicit rescan; the native owner keeps newer route/stop intent."""
        deadline = time.monotonic() + timeout
        errors = []
        for ticket in self.request("audio_retry"):
            if not ticket:
                continue
            try:
                self.wait_audio(ticket, timeout=max(0, deadline-time.monotonic()))
            except OSError as error:
                errors.append(str(error))
        return errors

    def close(self):
        import _winapi
        with self.lock:
            if self.closed:
                return
            if self.process and self.handle is not None:
                with contextlib.suppress(OSError, TimeoutError):
                    self.request("runtime_exit")
            self.closed = True
            if self.handle is not None:
                _winapi.CloseHandle(self.handle)
                self.handle = None
            if self.process:
                try:
                    self.process.wait(5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(2)
                if self.process.stderr:
                    self.process.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def current():
    global _client
    pipe = os.environ.get("BLUEHEAD_NATIVE_PIPE")
    if not pipe:
        return None
    with _client_lock:
        if _client is None:
            _client = NativeClient(pipe)
        return _client


class _Source:
    def __init__(self, client):
        self.client, self.sink = client, None
        self.failure = None
        self.failure_category = ''
        self.terminated = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self.poll())

    def set_packet_sink(self, sink):
        self.sink = sink

    async def poll(self):
        try:
            while True:
                batch = self.client.request("usb_poll")
                for packet in batch["packets"]:
                    if self.sink:
                        self.sink.on_packet(bytes.fromhex(packet))
                if not batch["active"]:
                    reason = batch.get('reason', '')
                    if reason and reason != 'device_removed':
                        self.failure_category = reason
                        self.failure = ConnectionError(
                            '蓝牙控制队列积压，接收已停止，请重新开始接收' if reason == 'control_backpressure' else
                            '蓝牙传输已停止，请重新开始接收')
                    break
                await asyncio.sleep(.004 if not batch["packets"] else 0)
        except asyncio.CancelledError:
            return
        except (OSError, ValueError) as error:
            self.failure = error
            self.failure_category = 'native_channel'
        finally:
            if not self.terminated.done():
                self.terminated.set_result(None)


class _Sink:
    def __init__(self, client):
        self.client = client

    def on_packet(self, packet):
        self.client.request("usb_send", packet=bytes(packet).hex())


class NativeTransport:
    def __init__(self, client, spec):
        match = re.fullmatch(r"usb:([0-9a-fA-F]{4}):([0-9a-fA-F]{4})#(\d{1,2})(?:\+sco=0)?", spec)
        if not match:
            raise ValueError("原生接收需要有效的 USB 适配器")
        self.client = client
        self.client.request("usb_open", vid=int(match[1], 16), pid=int(match[2], 16), index=int(match[3]))
        self.source, self.sink = _Source(client), _Sink(client)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, _error, _trace):
        self.source.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.source.task
        try:
            self.client.request("usb_close")
        except OSError:
            if exc_type is None:
                raise


class RemoteInput:
    def __init__(self, mixer, key, index, lease):
        self.mixer, self.key, self.index, self.lease = mixer, key, index, lease

    def value(self, field):
        return self.mixer.client.request("snapshot")["sources"].get(self.key, {}).get(field, 0)

    @property
    def frames(self):
        return self.value("queued_frames")

    @property
    def dropped(self):
        return self.value("dropped_frames")


class RemoteMixer:
    rate = 48000
    remote = True

    def __init__(self, client, buffer_ms=60, call_buffer_ms=30):
        self.client = client
        self.inputs, self.gains = {}, {}
        self.target, self.call_target = buffer_ms * 48, call_buffer_ms * 48
        self._master = 1.0
        self.call_active, self.call_source = False, None
        self.client.request("mix_config", buffer_ms=buffer_ms, call_buffer_ms=call_buffer_ms)

    @property
    def master(self):
        return self._master

    @master.setter
    def master(self, value):
        self._master = value
        self.client.request("master_gain", bus="playback", gain=value)

    def add(self, key):
        if key not in self.inputs:
            result = self.client.request("mix_add", key=key)
            self.inputs[key] = RemoteInput(self, key, result["index"], result["lease"])
            self.set_gain(key, self.gains.get(key, 1.0))

    def remove(self, key, lease=None):
        current = self.inputs.get(key)
        if current is not None:
            if lease is not None and current.lease != lease:
                return
            with contextlib.suppress(SourceExpired):
                self.client.request("mix_remove", key=key, lease=current.lease)
            if self.inputs.get(key) is current:
                self.inputs.pop(key, None)

    def clear(self, key):
        current = self.inputs.get(key)
        if current is not None:
            with contextlib.suppress(SourceExpired):
                self.client.request("mix_clear", key=key, lease=current.lease)

    def set_gain(self, key, value):
        self.gains[key] = value
        current = self.inputs.get(key)
        if current is not None:
            with contextlib.suppress(SourceExpired):
                self.client.request("mix_gain", key=key, gain=value, lease=current.lease)

    def set_enabled(self, key, enabled, lease=None):
        current = self.inputs.get(key)
        if current is not None:
            if lease is not None and current.lease != lease:
                return
            with contextlib.suppress(SourceExpired):
                self.client.request("mix_gate", key=key, enabled=bool(enabled), lease=current.lease)

    def snapshot(self):
        snapshot = self.client.request("snapshot")
        return {"native_mixer": True, "native_runtime": True, "media_buffer_ms": self.target // 48,
                "call_buffer_ms": self.call_target // 48, "underruns": sum(s["underruns"] for s in snapshot["sources"].values()),
                "callback_contentions": 0, "continuity": {**snapshot, "sources": list(snapshot["sources"].values())},
                "streams": [{"queued_ms": s["queued_frames"] / 48, "dropped_frames": s["dropped_frames"]}
                            for s in snapshot["sources"].values()]}


class RemoteOutput:
    def __init__(self, mixer, device):
        self.client, self.device = mixer.client, device
        self.closed, self.running = False, False
        self.endpoint_id = None
        self.stream = self

    def start(self):
        self.switch_device(self.device)
        self.running = True

    def switch_device(self, device, force=False):
        from .audio import resolve_endpoint
        endpoint = resolve_endpoint(device, "output")
        self.client.audio("output", endpoint, force=force)
        self.device, self.endpoint_id = device, endpoint

    def close(self):
        if not self.closed:
            self.client.audio("output", start=False)
            self.closed, self.running = True, False


class RemoteMediaSession:
    def __init__(self, key, mixer, emit, has_competitor=None):
        self.key, self.mixer, self.emit = key, mixer, emit
        self.client = mixer.client
        self.closed, self.paused, self.allowed = False, True, True
        self.channel = None
        self.format = None
        self.started_at = self.paused_at = 0.0
        self.mixer.add(key)
        self.input = self.mixer.inputs[key]

    def _request(self, op, **values):
        if self.closed:
            return None
        try:
            return self.client.request(op, key=self.key, lease=self.input.lease, **values)
        except SourceExpired:
            return None

    def configure(self, fmt, prepared=None):
        if prepared:
            prepared.close()
        code = {"sbc": 1, "aac": 2, "aptx": 3, "aptx_hd": 4, "ldac": 5}[fmt.codec]
        if not self._request("media_config", codec=code, rate=fmt.rate, channels=fmt.channels):
            return
        self.format = fmt
        if self.channel:
            self.bind(self.channel)
        if not self.paused:
            self.resume()
        self.emit("format", key=self.key, codec=fmt.codec, rate=fmt.rate, channels=fmt.channels)

    def bind(self, channel):
        if self.closed:
            return
        self.channel = channel
        self._request("media_route", handle=channel.connection.handle, cid=channel.source_cid)

    def unbind(self):
        self.channel = None
        if not self.closed:
            self._request("media_unroute")

    def push(self, data):
        # Audio is intercepted before Python HCI/L2CAP dispatch. A packet that
        # races registration is discarded; it never becomes a Python PCM path.
        pass

    def set_allowed(self, allowed):
        self.allowed = allowed
        self.mixer.set_enabled(self.key, allowed, lease=self.input.lease)

    def pause(self):
        if not self.closed:
            self.paused = True
            self.paused_at = time.monotonic()
            self._request("media_pause", paused=True)

    def resume(self):
        if not self.closed:
            self.paused = False
            self.started_at = time.monotonic()
            self._request("media_pause", paused=False)

    @property
    def last_packet(self):
        if self.closed:
            return 0.0
        data = self._request("media_snapshot")
        if data is None:
            return 0.0
        age = data.get("last_packet_age_ms", 1e12)
        return time.monotonic() - age / 1000

    def snapshot(self):
        data = self._request("media_snapshot")
        if data is None:
            return {}
        return {**data, "codec": self.format.codec if self.format else "", "rate": self.format.rate if self.format else 0,
                "selected": self.allowed, "protected": data.get("protected", False), "bitrate": data.get("bitrate", 0)}

    def close(self):
        if not self.closed:
            self.closed = True
            self.mixer.remove(self.key, lease=self.input.lease)

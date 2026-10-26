"""Lease-aware media and audio control facades; no sample processing."""
import contextlib
import time
from ..errors import SourceExpired

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
        from ..audio import resolve_endpoint
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

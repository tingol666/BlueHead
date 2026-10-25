"""Owned native A2DP decoder; no Python PCM processing on the production path."""
import ctypes as c
from pathlib import Path
import os


class NativeMedia:
    def __init__(self, fmt, output_rate=48000):
        self.fmt = fmt
        self.handle = None
        assets = Path(__file__).parent / "assets"
        self.directory = os.add_dll_directory(str(assets))
        try:
            self.api = c.CDLL(str(assets / "bluehead_audio.dll"))
            api = self.api
            api.bh_media_create.argtypes = [c.c_int] * 4
            api.bh_media_create.restype = c.c_void_p
            for name in ("bh_media_destroy", "bh_media_discard", "bh_media_restart"):
                getattr(api, name).argtypes = [c.c_void_p]
            api.bh_media_feed.argtypes = [c.c_void_p, c.c_void_p, c.c_uint]
            api.bh_media_read.argtypes = [c.c_void_p, c.c_void_p, c.c_uint]
            api.bh_media_read.restype = c.c_uint
            api.bh_media_drain_mixer.argtypes = [c.c_void_p, c.c_void_p, c.c_int]
            api.bh_media_drain_mixer.restype = c.c_uint
            api.bh_media_value.argtypes = [c.c_void_p, c.c_int]
            api.bh_media_value.restype = c.c_uint64
            codec = {"sbc": 1, "aac": 2, "aptx": 3, "aptx_hd": 4}[fmt.codec]
            self.handle = api.bh_media_create(codec, fmt.rate, fmt.channels, output_rate)
            if not self.handle:
                raise OSError("Native media codec could not initialize")
        except BaseException:
            self.directory.close()
            raise

    def value(self, field):
        return int(self.api.bh_media_value(self.handle, field))

    @property
    def frames(self):
        return self.value(0)

    @property
    def bytes(self):
        return self.value(1)

    @property
    def coded_bytes(self):
        return self.value(2)

    @property
    def errors(self):
        return self.value(3)

    @property
    def recoveries(self):
        return self.value(4)

    @property
    def lost(self):
        return self.value(5)

    @property
    def synchronizing(self):
        return bool(self.value(6))

    @property
    def depacketizer(self):
        # Telemetry compatibility; extraction and loss tracking are native.
        return self

    def restart(self):
        if self.api.bh_media_restart(self.handle) < 0:
            raise OSError("Native media restart failed")

    def decode_packet(self, data):
        if not isinstance(data, bytes) or not 1 <= len(data) <= 65535:
            raise ValueError("Invalid media packet")
        if self.api.bh_media_feed(self.handle, data, len(data)) < 0:
            from .protocol import PacketError
            raise PacketError("Native media rejected a damaged packet")

    def discard_pcm(self):
        self.api.bh_media_discard(self.handle)

    def write_to(self, mixer, key):
        stream = mixer.inputs.get(key)
        if stream is None:
            self.discard_pcm()
            return 0
        return int(self.api.bh_media_drain_mixer(self.handle, mixer.native.handle, stream.index))

    def read_pcm(self):
        frames = self.value(7)
        output = (c.c_float * (frames * 2))()
        frames = self.api.bh_media_read(self.handle, output, frames)
        return c.string_at(output, frames * 8)

    def feed(self, data):
        """Development array facade. Release processing uses decode_packet/write_to."""
        self.decode_packet(data)
        pcm = self.read_pcm()
        if not pcm:
            return []
        import numpy as np
        return [np.frombuffer(pcm, np.float32).reshape(-1, 2).copy()]

    def close(self):
        if self.handle:
            self.api.bh_media_destroy(self.handle)
            self.handle = None
        if getattr(self, "directory", None):
            self.directory.close()
            self.directory = None

    def __del__(self):
        self.close()

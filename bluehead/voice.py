"""Thin native HFP voice transport: mono S16/48k PCM, no Python DSP.

Each instance serializes its C context, including close, so a late worker call
cannot race destruction. No method should run inside an OS audio callback.
"""

from __future__ import annotations
import ctypes as c
import os
from pathlib import Path
import threading

PCM_SAMPLES = 360
PCM_BYTES = PCM_SAMPLES * 2
QUEUE_SAMPLES = PCM_SAMPLES * 32
CODEC_NAMES = {1: "CVSD", 2: "mSBC", 3: "LC3-SWB"}


class _Stats(c.Structure):
    _fields_ = (
        [("abi_version", c.c_uint32), ("codec_id", c.c_uint32)]
        + [
            (name, c.c_uint64)
            for name in (
                "rx_packets",
                "rx_bytes",
                "rx_frames",
                "tx_frames",
                "plc_frames",
                "missing_frames",
                "corrupt_frames",
                "duplicate_frames",
                "resync_bytes",
                "queue_overruns",
                "discarded_samples",
            )
        ]
        + [("queued_samples", c.c_uint32), ("high_water_samples", c.c_uint32)]
    )


def _load():
    assets = Path(__file__).parent / "assets"
    directory = os.add_dll_directory(str(assets))
    try:
        api = c.CDLL(str(assets / "bluehead_voice.dll"))
        api.bh_voice_abi_version.restype = c.c_uint32
        if api.bh_voice_abi_version() != 1:
            raise OSError("Unsupported BlueHead voice engine ABI")
        api.bh_voice_codec_available.argtypes = [c.c_uint32]
        api.bh_voice_codec_available.restype = c.c_uint32
        api.bh_voice_create.argtypes = [c.c_uint32]
        api.bh_voice_create.restype = c.c_void_p
        api.bh_voice_destroy.argtypes = [c.c_void_p]
        api.bh_voice_destroy.restype = None
        api.bh_voice_reset.argtypes = [c.c_void_p]
        api.bh_voice_encode.argtypes = [c.c_void_p, c.c_void_p, c.c_uint32, c.c_void_p, c.c_uint32]
        api.bh_voice_push_rx.argtypes = [c.c_void_p, c.c_void_p, c.c_uint32, c.c_uint32]
        api.bh_voice_read_pcm.argtypes = [c.c_void_p, c.c_void_p, c.c_uint32]
        api.bh_voice_read_pcm.restype = c.c_uint32
        api.bh_voice_conceal.argtypes = [c.c_void_p, c.c_uint32]
        api.bh_voice_get_stats.argtypes = [c.c_void_p, c.POINTER(_Stats), c.c_uint32]
        return api, directory
    except BaseException:
        directory.close()
        raise


def available_codecs() -> tuple[int, ...]:
    """Report compiled codec support; initialization errors still fail explicitly."""
    api, directory = _load()
    try:
        return tuple(codec for codec in CODEC_NAMES if api.bh_voice_codec_available(codec))
    finally:
        directory.close()


class VoiceCodec:
    pcm_rate = 48000
    pcm_frame_samples = PCM_SAMPLES
    pcm_frame_bytes = PCM_BYTES

    def __init__(self, codec_id: int):
        if type(codec_id) is not int or codec_id not in CODEC_NAMES:
            raise ValueError("Unsupported HFP codec ID")
        self.codec_id = codec_id
        self.name = CODEC_NAMES[codec_id]
        self.wire_frame_bytes = 120 if codec_id == 1 else 60
        self.handle = None
        self.directory = None
        self.lock = threading.RLock()
        self.api, self.directory = _load()
        try:
            self.handle = self.api.bh_voice_create(codec_id)
            if not self.handle:
                raise OSError(f"Native {self.name} codec could not initialize")
        except BaseException:
            self.close()
            raise

    def _require_open(self):
        if not self.handle:
            raise RuntimeError("Voice codec is closed")

    def encode(self, pcm: bytes) -> bytes:
        if not isinstance(pcm, bytes) or len(pcm) != PCM_BYTES:
            raise ValueError("Voice encoding requires exactly 720 bytes of 48k mono S16 PCM")
        with self.lock:
            self._require_open()
            output = c.create_string_buffer(self.wire_frame_bytes)
            length = self.api.bh_voice_encode(self.handle, pcm, PCM_SAMPLES, output, len(output))
            if length != self.wire_frame_bytes:
                raise OSError(f"Native {self.name} encoding failed ({length})")
            return output.raw

    def push_rx(self, data: bytes, status: int = 0) -> int:
        if not isinstance(data, bytes) or len(data) > 4096 or type(status) is not int or not 0 <= status <= 3:
            raise ValueError("Invalid HCI synchronous packet")
        with self.lock:
            self._require_open()
            result = self.api.bh_voice_push_rx(self.handle, data, len(data), status)
            if result < 0:
                raise OSError(f"Native {self.name} receive failed ({result})")
            return result

    def read_pcm(self, max_samples: int = QUEUE_SAMPLES) -> bytes:
        if type(max_samples) is not int or not 0 <= max_samples <= QUEUE_SAMPLES:
            raise ValueError("Invalid voice PCM read capacity")
        with self.lock:
            self._require_open()
            output = c.create_string_buffer(max_samples * 2)
            count = self.api.bh_voice_read_pcm(self.handle, output, max_samples)
            if count > max_samples:
                raise OSError("Native voice returned an invalid PCM length")
            return output.raw[: count * 2]

    def conceal(self, frames: int = 1) -> int:
        if type(frames) is not int or not 0 <= frames <= 8:
            raise ValueError("Invalid missing voice frame count")
        with self.lock:
            self._require_open()
            result = self.api.bh_voice_conceal(self.handle, frames)
            if result < 0:
                raise OSError("Native voice concealment failed")
            return result

    def stats(self) -> dict[str, int]:
        with self.lock:
            self._require_open()
            stats = _Stats()
            if self.api.bh_voice_get_stats(self.handle, c.byref(stats), c.sizeof(stats)):
                raise OSError("Native voice telemetry failed")
            return {name: int(getattr(stats, name)) for name, _ in _Stats._fields_}

    def reset(self):
        with self.lock:
            self._require_open()
            if self.api.bh_voice_reset(self.handle):
                raise OSError("Native voice reset failed")

    def close(self):
        with self.lock:
            if self.handle:
                self.api.bh_voice_destroy(self.handle)
                self.handle = None
            if self.directory:
                self.directory.close()
                self.directory = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        if hasattr(self, "lock"):
            self.close()

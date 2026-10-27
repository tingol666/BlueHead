"""Deterministic, self-generated audio; no copyrighted test recordings."""
import ctypes
from pathlib import Path

import av
import numpy as np
from bumble.codecs import AacAudioRtpPacket
from bumble.rtp import MediaPacket


def signal(rate, seconds=0.35, channels=2):
    rng = np.random.default_rng(20261024)
    t = np.arange(int(rate * seconds)) / rate
    data = np.zeros((len(t), channels), dtype=np.float64)
    for c in range(channels):
        for f, phase in zip(rng.uniform(150, min(6000, rate * 0.35), 16), rng.uniform(0, 6.28, 16)):
            data[:, c] += np.sin(t * f * 2 * np.pi + phase)
    data *= 0.6 / np.max(np.abs(data))
    return data.astype(np.float32)


def rtp(payload, sequence, timestamp=0):
    return bytes(MediaPacket(2, 0, 0, 1, sequence & 65535, timestamp, 0, [], 96, payload))


def encode(codec, pcm, rate=48000):
    context = av.CodecContext.create(codec, "w")
    context.sample_rate = rate
    context.layout = "stereo"
    context.format = {"sbc": "s16", "aac": "fltp", "aptx": "s32p", "aptx_hd": "s32p"}[codec]
    context.bit_rate = 320000 if codec == "aac" else 328000
    context.open()
    packets = []
    for offset in range(0, len(pcm) - context.frame_size + 1, context.frame_size):
        block = pcm[offset:offset + context.frame_size]
        if context.format.name == "s16":
            data = np.round(block * 32767).astype(np.int16).reshape(1, -1)
        elif context.format.name == "s32p":
            data = np.round(block.T * 2147483647).astype(np.int32).copy()
        else:
            data = block.T.copy()
        frame = av.AudioFrame.from_ndarray(data, context.format.name, "stereo")
        frame.sample_rate = rate
        packets.extend(context.encode(frame))
    packets.extend(context.encode(None))
    return [bytes(p) for p in packets]


def wire_packets(codec, packets, rate=48000):
    timestamp = 0
    for sequence, p in enumerate(packets):
        if codec == "aac":
            duration = 1024
            p = bytes(AacAudioRtpPacket.for_simple_aac(rate, 2, p))
        elif codec == "sbc":
            duration = 128
            p = b"\x01" + p
        elif codec == "ldac":
            raise ValueError("use LDAC packet groups directly")
        else:
            # One aptX codeword covers four samples, but one RTP packet can
            # contain many codewords. Advance by the whole packet duration.
            duration = len(p) // (6 if codec == "aptx_hd" else 4) * 4
        yield p if codec == "aptx" else rtp(p, sequence, timestamp)
        timestamp += duration


def quality(reference, decoded):
    # Find codec algorithm delay, independent of the pipeline being tested.
    n = 1 << (len(reference) + len(decoded) - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(decoded[:, 0], n) * np.conj(np.fft.rfft(reference[:, 0], n)), n)
    delay = int(np.argmax(corr[:min(4096, len(decoded) // 2)]))
    count = min(len(reference), len(decoded) - delay)
    # Exclude start/flush transients, not steady-state distortion.
    trim = min(1200, count // 5)
    ref = reference[trim:count - trim].astype(np.float64)
    out = decoded[delay + trim:delay + count - trim].astype(np.float64)
    if len(ref) < 100:
        return -100.0, delay
    snr = 10 * np.log10(np.mean(ref ** 2) / max(np.mean((ref - out) ** 2), 1e-20))
    return float(snr), delay


class LdacEncoder:
    def __init__(self, rate=48000, quality_mode=0):
        self.lib = ctypes.CDLL(str(Path(__file__).parent / "ldac_encoder.dll"))
        self.lib.ldacBT_get_handle.restype = ctypes.c_void_p
        self.lib.ldacBT_init_handle_encode.argtypes = [ctypes.c_void_p] + [ctypes.c_int] * 5
        self.lib.ldacBT_encode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self.lib.ldacBT_free_handle.argtypes = [ctypes.c_void_p]
        self.handle = self.lib.ldacBT_get_handle()
        # Stereo=1; float32=5; LDACBT_ENC_LSU=128 samples, both rates.
        result = self.lib.ldacBT_init_handle_encode(self.handle, 1005, quality_mode, 1, 5, rate)
        if result != 0:
            raise RuntimeError(f"LDAC encoder init: {result}")

    def encode(self, pcm):
        result = []
        offset = 0
        while offset + 128 <= len(pcm):
            block = np.asarray(pcm[offset:offset + 128], dtype=np.float32)
            used, size, frames = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
            output = ctypes.create_string_buffer(2048)
            ret = self.lib.ldacBT_encode(self.handle, block.ctypes.data, ctypes.byref(used), output, ctypes.byref(size), ctypes.byref(frames))
            if ret != 0 or used.value <= 0:
                raise RuntimeError(f"LDAC encoder failed: {ret}")
            offset += used.value // 8
            if size.value:
                result.append(bytes([frames.value]) + output.raw[:size.value])
        return result

    def close(self):
        self.lib.ldacBT_free_handle(self.handle)

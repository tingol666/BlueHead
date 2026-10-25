"""Exercise the actual C LC3 codec and H2 stream parser, without audio devices."""

from __future__ import annotations
import ctypes as C
import os
from pathlib import Path
import random

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
FRAME = 360


class Stats(C.Structure):
    _fields_ = (
        [("abi_version", C.c_uint32), ("codec_id", C.c_uint32)]
        + [
            (name, C.c_uint64)
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
        + [("queued_samples", C.c_uint32), ("high_water_samples", C.c_uint32)]
    )


@pytest.fixture(scope="module")
def dll():
    # Standalone source-tree tests use the same pinned FFmpeg binaries. Frozen
    # application packaging places its dependencies on the restricted DLL path.
    directories = [ROOT / "bluehead/assets", ROOT / "work/ffmpeg-audio/install/bin"]
    handles = [os.add_dll_directory(str(path)) for path in directories if path.is_dir()]
    result = C.CDLL(str(ROOT / "bluehead/assets/bluehead_voice.dll"))
    result.bh_voice_abi_version.restype = C.c_uint32
    result.bh_voice_codec_available.argtypes = [C.c_uint32]
    result.bh_voice_codec_available.restype = C.c_uint32
    result.bh_voice_create.argtypes = [C.c_uint32]
    result.bh_voice_create.restype = C.c_void_p
    result.bh_voice_destroy.argtypes = [C.c_void_p]
    result.bh_voice_reset.argtypes = [C.c_void_p]
    result.bh_voice_encode.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32]
    result.bh_voice_push_rx.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32, C.c_uint32]
    result.bh_voice_read_pcm.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32]
    result.bh_voice_read_pcm.restype = C.c_uint32
    result.bh_voice_conceal.argtypes = [C.c_void_p, C.c_uint32]
    result.bh_voice_get_stats.argtypes = [C.c_void_p, C.POINTER(Stats), C.c_uint32]
    assert result.bh_voice_abi_version() == 1
    yield result
    for handle in handles:
        handle.close()


class Voice:
    def __init__(self, dll, codec=3):
        self.dll = dll
        self.handle = dll.bh_voice_create(codec)
        assert self.handle

    def close(self):
        if self.handle:
            self.dll.bh_voice_destroy(self.handle)
            self.handle = None

    def encode(self, pcm):
        pcm = np.ascontiguousarray(pcm, dtype=np.int16)
        out = (C.c_uint8 * 120)()
        length = self.dll.bh_voice_encode(self.handle, pcm.ctypes.data, len(pcm), out, len(out))
        assert length > 0
        return bytes(out[:length])

    def push(self, wire, status=0):
        out = C.create_string_buffer(wire)
        result = self.dll.bh_voice_push_rx(self.handle, out, len(wire), status)
        assert result >= 0
        return result

    def read(self, capacity=32 * FRAME):
        out = np.zeros(capacity, dtype=np.int16)
        count = self.dll.bh_voice_read_pcm(self.handle, out.ctypes.data, capacity)
        return out[:count].copy()

    def stats(self):
        stats = Stats()
        assert self.dll.bh_voice_get_stats(self.handle, C.byref(stats), C.sizeof(stats)) == 0
        return stats


@pytest.fixture
def voice(dll):
    result = Voice(dll)
    yield result
    result.close()


def tone(index, frequency=997, amplitude=10000):
    return np.rint(amplitude * np.sin(2 * np.pi * frequency * (np.arange(FRAME) + index * FRAME) / 48000)).astype(
        np.int16
    )


def test_codec_real_roundtrip_h2_and_spectrum(voice):
    samples = []
    for index in range(100):
        wire = voice.encode(tone(index))
        assert len(wire) == 60
        assert wire[:2] == bytes((1, (0x08, 0x38, 0xC8, 0xF8)[index % 4]))
        assert voice.push(wire) == 1
        samples.append(voice.read())
    decoded = np.concatenate(samples).astype(np.float64)[FRAME * 10 :]
    assert 4500 < np.sqrt(np.mean(decoded * decoded)) < 8500
    spectrum = np.abs(np.fft.rfft(decoded))
    frequencies = np.fft.rfftfreq(len(decoded), 1 / 48000)
    assert abs(frequencies[spectrum.argmax()] - 997) < 3
    assert voice.stats().plc_frames == 0


@pytest.mark.parametrize("split", range(1, 60))
def test_every_split_boundary(voice, split):
    wire = voice.encode(tone(0))
    assert voice.push(wire[:split]) == 0
    assert len(voice.read()) == 0
    assert voice.push(wire[split:]) == 1
    assert len(voice.read()) == FRAME


def test_fragmented_multiframe_stream_with_junk(voice):
    wire = b"\x00\x02\x01\x01\x10" + b"".join(voice.encode(tone(i)) for i in range(27))
    rng = random.Random(1234)
    count = 0
    while wire:
        size = rng.randrange(1, 124)
        count += voice.push(wire[:size])
        wire = wire[size:]
    assert count == 27
    assert len(voice.read()) == 27 * FRAME
    assert voice.stats().resync_bytes == 5


@pytest.mark.parametrize("missing", [1, 2, 3])
def test_sequence_gap_invokes_actual_plc(voice, missing):
    frames = [voice.encode(tone(i)) for i in range(missing + 2)]
    voice.push(frames[0])
    voice.read()
    assert voice.push(frames[-1]) == missing + 1
    assert len(voice.read()) == (missing + 1) * FRAME
    stats = voice.stats()
    assert stats.missing_frames == stats.plc_frames == missing


def test_packet_status_marks_partial_frame_bad_without_double_plc(voice):
    wire = voice.encode(tone(0))
    voice.push(wire[:17])
    voice.push(wire[17:38], status=1)
    voice.push(wire[38:])
    assert len(voice.read()) == FRAME
    assert voice.push(voice.encode(tone(1))) == 1
    stats = voice.stats()
    assert stats.corrupt_frames == stats.plc_frames == 1
    assert stats.missing_frames == 0


def test_explicit_loss_does_not_double_conceal(voice):
    frames = [voice.encode(tone(i)) for i in range(5)]
    voice.push(frames[0])
    voice.read()
    assert voice.dll.bh_voice_conceal(voice.handle, 3) == 3
    voice.read()
    assert voice.push(frames[4]) == 1
    assert voice.stats().plc_frames == 3


def test_duplicate_frame_is_ignored(voice):
    wire = voice.encode(tone(0))
    voice.push(wire)
    voice.read()
    assert voice.push(wire) == 0
    assert len(voice.read()) == 0
    assert voice.stats().duplicate_frames == 1


def test_queue_is_bounded_and_partial_reads_are_safe(voice):
    for i in range(100):
        voice.push(voice.encode(tone(i)))
        if i == 16:
            assert len(voice.read(7)) == 7
    stats = voice.stats()
    assert stats.queued_samples <= 32 * FRAME
    assert stats.high_water_samples <= 32 * FRAME
    assert stats.queue_overruns > 0
    assert np.isfinite(voice.read()).all()


def test_reset_and_argument_errors_do_not_consume_state(voice, dll):
    pcm = tone(0)
    out = (C.c_uint8 * 60)()
    assert dll.bh_voice_encode(voice.handle, pcm.ctypes.data, FRAME, out, 59) == -2
    assert dll.bh_voice_encode(voice.handle, pcm.ctypes.data, FRAME - 1, out, 60) == -1
    assert voice.encode(pcm)[1] == 0x08
    assert dll.bh_voice_push_rx(voice.handle, None, 1, 0) == -1
    assert dll.bh_voice_push_rx(voice.handle, None, 0, 4) == -1
    assert dll.bh_voice_conceal(voice.handle, 9) == -1
    assert dll.bh_voice_get_stats(voice.handle, C.byref(Stats()), 1) == -1
    assert dll.bh_voice_reset(voice.handle) == 0
    assert voice.stats().tx_frames == 0
    assert voice.encode(pcm)[1] == 0x08
    assert not dll.bh_voice_create(0)
    assert not dll.bh_voice_create(999)


def test_real_codec_long_stream_has_no_drift_or_unbounded_queues(voice):
    # 30 seconds of actual speech-codec execution, deliberately odd packet sizes.
    expected = 4000 * FRAME
    received = 0
    for i in range(4000):
        wire = voice.encode(tone(i))
        for start in range(0, 60, 17):
            voice.push(wire[start : start + 17])
        received += len(voice.read(997))
    assert received == expected
    assert voice.stats().queued_samples == 0
    assert voice.stats().rx_frames == 4000
    assert voice.stats().plc_frames == 0
    assert voice.stats().queue_overruns == 0


@pytest.fixture(params=[1, 2, 3])
def voice_codec(dll, request):
    assert dll.bh_voice_codec_available(request.param)
    result = Voice(dll, request.param)
    yield result
    result.close()


def test_all_native_codecs_have_48k_pcm_and_stable_timing(voice_codec):
    voice = voice_codec
    codec = voice.stats().codec_id
    samples = []
    for index in range(100):
        wire = voice.encode(tone(index))
        assert len(wire) == (120 if codec == 1 else 60)
        if codec == 2:
            assert wire[2:5] == b"\xad\x00\x00"
            assert wire[59] == 0
        assert voice.push(wire) == 1
        pcm = voice.read()
        assert len(pcm) == FRAME
        samples.append(pcm)
    decoded = np.concatenate(samples).astype(np.float64)[FRAME * 10 :]
    assert 4500 < np.sqrt(np.mean(decoded * decoded)) < 8500
    spectrum = np.abs(np.fft.rfft(decoded))
    frequencies = np.fft.rfftfreq(len(decoded), 1 / 48000)
    assert abs(frequencies[spectrum.argmax()] - 997) < 3
    assert voice.stats().plc_frames == 0


def test_all_codec_byte_fragmentation(voice_codec):
    voice = voice_codec
    for i in range(7):
        wire = voice.encode(tone(i))
        for value in wire:
            voice.push(bytes([value]))
        assert len(voice.read()) == FRAME
    assert voice.stats().rx_frames == 7


def test_all_codec_damaged_packet_plc_and_recovery(voice_codec):
    voice = voice_codec
    for i in range(40):
        wire = voice.encode(tone(i, 257))
        voice.push(wire, status=1 if 20 <= i < 23 else 0)
        decoded = voice.read()
        assert len(decoded) == FRAME
        assert np.max(np.abs(decoded.astype(np.int32))) <= 32768
        if 21 <= i < 23:
            # This executes SpanDSP for CVSD/mSBC and liblc3 for LC3, and must
            # produce an actual concealment waveform rather than drop time.
            assert np.count_nonzero(decoded) > FRAME // 2
    assert voice.stats().corrupt_frames == 3
    assert voice.stats().plc_frames == 3


def test_all_codec_reset_restores_exact_initial_encoder_state(voice_codec):
    voice = voice_codec
    initial = voice.encode(tone(0))
    for i in range(10):
        voice.push(voice.encode(tone(i)))
    assert voice.dll.bh_voice_reset(voice.handle) == 0
    assert voice.encode(tone(0)) == initial
    assert voice.stats().tx_frames == 1
    assert len(voice.read()) == 0


@pytest.mark.parametrize("size", [1, 3, 17, 24, 48, 60, 83, 120])
def test_cvsd_arbitrary_hci_packet_sizes(dll, size):
    voice = Voice(dll, 1)
    try:
        wire = b"".join(voice.encode(tone(i)) for i in range(20))
        count = 0
        for offset in range(0, len(wire), size):
            count += voice.push(wire[offset : offset + size])
        assert count == 20
        assert len(voice.read()) == 20 * FRAME
    finally:
        voice.close()


@pytest.mark.parametrize("codec", [1, 2, 3])
def test_python_transport_wrapper_has_owned_bytes_and_safe_close(dll, codec):
    from bluehead.voice import VoiceCodec, available_codecs

    assert available_codecs() == (1, 2, 3)
    with VoiceCodec(codec) as voice:
        packet = voice.encode(tone(0).tobytes())
        voice.push_rx(packet[:7])
        voice.push_rx(packet[7:])
        assert len(voice.read_pcm()) == 720
        assert voice.stats()["rx_frames"] == 1
        voice.reset()
        assert voice.stats()["tx_frames"] == 0
        with pytest.raises(ValueError):
            voice.encode(bytes(360))
        with pytest.raises(ValueError):
            voice.read_pcm(20000)
        with pytest.raises(ValueError):
            voice.push_rx(bytes(4097))
    voice.close()
    with pytest.raises(RuntimeError, match="closed"):
        voice.encode(bytes(720))
    with pytest.raises(RuntimeError, match="closed"):
        voice.stats()


@pytest.mark.parametrize("codec", [2, 3])
def test_bad_h2_header_resynchronizes_and_conceals_missing_frame(dll, codec):
    voice = Voice(dll, codec)
    try:
        packets = [voice.encode(tone(i)) for i in range(4)]
        voice.push(packets[0])
        voice.read()
        corrupt = bytearray(packets[1])
        corrupt[1] = 0xFF
        voice.push(bytes(corrupt), status=1)
        assert voice.push(packets[2]) == 2
        assert len(voice.read()) == FRAME * 2
        assert voice.push(packets[3]) == 1
        assert voice.stats().missing_frames == 1
    finally:
        voice.close()


def test_msbc_receive_only_reset_clears_decoder_synthesis_history(dll):
    encoder, decoder = Voice(dll, 2), Voice(dll, 2)
    try:
        packets = [encoder.encode(tone(i)) for i in range(30)]
        decoder.push(packets[0])
        original = decoder.read()
        for packet in packets[1:]:
            decoder.push(packet)
            decoder.read()
        assert decoder.stats().tx_frames == 0
        assert decoder.dll.bh_voice_reset(decoder.handle) == 0
        decoder.push(packets[0])
        np.testing.assert_array_equal(decoder.read(), original)
    finally:
        encoder.close()
        decoder.close()


def test_untrusted_voice_bytes_stay_bounded_and_recover(voice_codec):
    voice = voice_codec
    rng = random.Random(89432)
    for _ in range(700):
        data = rng.randbytes(rng.randrange(1, 1024))
        voice.push(data, status=rng.randrange(4))
        assert len(voice.read()) <= 32 * FRAME
    assert voice.stats().high_water_samples <= 32 * FRAME
    assert voice.dll.bh_voice_reset(voice.handle) == 0
    assert voice.push(voice.encode(tone(0))) == 1
    assert len(voice.read()) == FRAME

"""Offline LDAC RTP/native SRC coverage; no Bluetooth or audio device access."""
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

from bluehead.ldac import IsolatedMedia, LdacDecoder, NativeLdac, MAX_PCM
from bluehead.protocol import Format, PacketError, ldac_frames
from .vectors import LdacEncoder, quality, rtp, signal


def _stalled_worker(pipe, *_):
    pipe.send_bytes(b"READY")
    time.sleep(10)
    pipe.close()


def fractional_quality(reference, decoded):
    """Allow the codec/SRC delay to be fractional at 44.1 -> 48 kHz.

    Integer-only alignment reports phase error as distortion. Fourier shifting
    the independent reference avoids giving the production SRC a reference role.
    """
    _, delay = quality(reference, decoded)
    size = 1 << len(reference).bit_length()
    spectrum = np.fft.rfft(reference, n=size, axis=0)
    frequencies = np.fft.rfftfreq(size)[:, None]
    count = min(len(reference), len(decoded) - delay)
    trim = 1200
    output = decoded[delay + trim:delay + count - trim].astype(np.float64)
    best = -100.
    for fraction in np.linspace(-.5, .5, 101):
        shifted = np.fft.irfft(spectrum * np.exp(-2j * np.pi * frequencies * fraction), n=size, axis=0)
        target = shifted[trim:count - trim]
        snr = 10 * np.log10(np.mean(target ** 2) / max(np.mean((target - output) ** 2), 1e-20))
        best = max(best, float(snr))
    return best, delay


def packets(rate=48000, mode=0, seconds=.6):
    encoder = LdacEncoder(rate, mode)
    try:
        payloads = encoder.encode(signal(rate, seconds))
    finally:
        encoder.close()
    return [rtp(payload, index) for index, payload in enumerate(payloads)]


@pytest.mark.parametrize("rate", [44100, 48000, 88200, 96000])
@pytest.mark.parametrize("mode", [0, 1, 2], ids=["high", "standard", "mobile"])
def test_real_ldac_encoder_all_rates_and_bitrates_native_48k_quality(rate, mode):
    decoder = IsolatedMedia(Format("ldac", rate, 2))
    try:
        output = []
        for packet in packets(rate, mode):
            decoder.decode_packet(packet)
            output.append(decoder.read_pcm())
        decoded = np.frombuffer(b"".join(output), np.float32).reshape(-1, 2)
        assert np.isfinite(decoded).all()
        assert .5 * 48000 < len(decoded) <= .6 * 48000
        snr, delay = fractional_quality(signal(48000, .6), decoded)
        assert snr > 25, (rate, mode, snr, delay)
        assert decoder.frames == len(decoded) and decoder.bytes > decoder.coded_bytes > 0
        assert decoder.errors == decoder.lost == decoder.value(9) == 0
    finally:
        decoder.close()


def test_reset_same_worker_sequence_and_native_src_state():
    decoder = IsolatedMedia(Format("ldac", 44100, 2))
    data = packets(44100, seconds=.15)
    try:
        first = []
        for packet in data:
            decoder.decode_packet(packet)
            first.append(decoder.read_pcm())
        pid, frames = decoder.process.pid, decoder.frames
        decoder.restart()
        assert decoder.process.pid == pid and not decoder.read_pcm()
        second = []
        for packet in data:
            decoder.decode_packet(packet)
            second.append(decoder.read_pcm())
        np.testing.assert_allclose(np.frombuffer(b"".join(first), np.float32),
                                   np.frombuffer(b"".join(second), np.float32), atol=1e-6, rtol=1e-5)
        assert decoder.frames == frames * 2 and decoder.lost == 0
    finally:
        decoder.close()


def test_rtp_loss_duplicate_late_and_wrap_match_depacketizer_rules():
    decoder = IsolatedMedia(Format("ldac", 48000, 2))
    payload = packets(seconds=.1)[0][12:]
    try:
        decoder.decode_packet(rtp(payload, 65534))
        frames = decoder.frames
        decoder.decode_packet(rtp(payload, 65534))
        decoder.decode_packet(rtp(payload, 65533))
        assert decoder.frames == frames
        decoder.decode_packet(rtp(payload, 0))
        assert decoder.lost == 1 and decoder.frames == frames * 2
        decoder.decode_packet(rtp(payload, 2))
        assert decoder.lost == 2
    finally:
        decoder.close()


def test_structural_invalid_packets_do_not_emit_partial_audio_and_worker_survives():
    decoder = IsolatedMedia(Format("ldac", 48000, 2))
    valid = packets(seconds=.1)[0]
    payload = valid[12:]
    malformed = [b"a", rtp(b"\x00", 1), rtp(b"\xe1" + payload[1:], 2),
                 rtp(payload[:-1], 3), rtp(payload + b"trailing", 4),
                 rtp(bytes([(payload[0] & 31) + 1]) + payload[1:] + b"\xaa", 5),
                 b"\x90" + valid[1:12] + b"\x00\x00\xff\xff",  # extension overrun
                 b"\xa0" + valid[1:12] + b"\x01\x00"]  # padding zero
    try:
        pid = decoder.process.pid
        for bad in malformed:
            with pytest.raises(PacketError):
                decoder.decode_packet(bad)
            assert not decoder.read_pcm()
        assert decoder.process.pid == pid
        decoder.restart()
        decoder.decode_packet(valid)
        assert decoder.read_pcm() and decoder.value(9) == len(malformed)
    finally:
        decoder.close()


def test_bad_codec_frame_resets_decoder_and_src_without_restarting_process():
    decoder = IsolatedMedia(Format("ldac", 48000, 2))
    valid = packets(seconds=.1)[0]
    frames = ldac_frames(valid[12:], decoder.fmt)
    bad = bytes([1]) + frames[0][:3] + bytes([255]) * (len(frames[0]) - 3)
    try:
        pid = decoder.process.pid
        with pytest.raises(PacketError):
            decoder.decode_packet(rtp(bad, 0))
        assert decoder.errors and not decoder.read_pcm()
        decoder.decode_packet(rtp(valid[12:], 1))
        assert decoder.process.pid == pid and decoder.read_pcm()
        assert decoder.recoveries == 1
    finally:
        decoder.close()


def test_worker_crash_recovers_on_next_packet_preserving_cumulative_telemetry():
    decoder = IsolatedMedia(Format("ldac", 48000, 2))
    data = packets(seconds=.1)
    try:
        decoder.decode_packet(data[0])
        before, pid = decoder.frames, decoder.process.pid
        decoder.process.terminate()
        decoder.process.join(1)
        with pytest.raises((RuntimeError, TimeoutError)):
            decoder.decode_packet(data[1])
        assert decoder.process is None and not decoder.read_pcm()
        decoder.decode_packet(data[2])
        assert decoder.process.pid != pid and decoder.frames > before and decoder.read_pcm()
        assert decoder.errors == decoder.recoveries == 1
    finally:
        decoder.close()


def test_parent_queue_bound_discard_and_native_float_writer():
    decoder = IsolatedMedia(Format("ldac", 48000, 2))
    data = packets(seconds=.6)
    written = []
    mixer = SimpleNamespace(write_float_bytes=lambda key, pcm: written.append((key, pcm)))
    try:
        for packet in data:
            decoder.decode_packet(packet)
        for index, packet in enumerate(data):
            decoder.decode_packet(rtp(packet[12:], len(data) + index))
        assert decoder.value(7) * 8 == MAX_PCM and decoder.value(8) > 0
        result = decoder.write_to(mixer, "device")
        assert result == MAX_PCM // 8 and len(written[0][1]) == MAX_PCM
        assert not decoder.read_pcm()
        decoder.decode_packet(rtp(data[0][12:], len(data) * 2))
        decoder.discard_pcm()
        assert decoder.value(7) == 0
    finally:
        decoder.close()


def test_legacy_raw_array_facades_are_compatible_and_lazy():
    packet = packets(seconds=.1)[0]
    frame = ldac_frames(packet[12:], Format("ldac", 48000, 2))[0]
    native, isolated = NativeLdac(), LdacDecoder(48000)
    try:
        expected, decoded = native.decode(frame), isolated.decode(frame)
        np.testing.assert_array_equal(expected, decoded)
        pid = isolated.process.pid
        isolated.reset()
        assert isolated.process.pid == pid
        assert isolated.decode(frame).shape == (128, 2)
    finally:
        native.close()
        isolated.close()


def test_production_module_import_has_no_pyav_or_numpy():
    script = "import sys,json; import bluehead.ldac; print(json.dumps({n:n in sys.modules for n in ['av','numpy']}))"
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                            check=True, text=True, capture_output=True)
    assert json.loads(result.stdout) == {"av": False, "numpy": False}


def test_worker_stall_bounds_large_pipe_write_and_recovers(monkeypatch):
    import bluehead.ldac as module
    original = module._worker
    monkeypatch.setattr(module, "_worker", _stalled_worker)
    decoder = IsolatedMedia(Format("ldac", 48000, 2))
    try:
        started = time.monotonic()
        # The child never reads: this checks the send deadline as well as receive.
        with pytest.raises(TimeoutError):
            decoder.decode_packet(bytes(65535))
        assert time.monotonic() - started < 2
        assert decoder.process is None
        monkeypatch.setattr(module, "_worker", original)
        decoder.decode_packet(packets(seconds=.1)[0])
        assert decoder.read_pcm() and decoder.errors == decoder.recoveries == 1
    finally:
        decoder.close()


@pytest.mark.parametrize("output_rate", [8000, 44100, 96000, 192000])
def test_native_output_rate_bound_and_frame_count(output_rate):
    decoder = IsolatedMedia(Format("ldac", 96000, 2), output_rate)
    try:
        output = []
        for packet in packets(96000, seconds=.2):
            decoder.decode_packet(packet)
            output.append(decoder.read_pcm())
        decoded = np.frombuffer(b"".join(output), np.float32)
        assert .15 * output_rate < len(decoded) // 2 <= .2 * output_rate
        assert np.isfinite(decoded).all()
    finally:
        decoder.close()


def test_damaged_packet_random_corpus_cannot_crash_isolated_decoder():
    decoder = IsolatedMedia(Format("ldac", 48000, 2))
    generator = np.random.default_rng(43)
    try:
        pid = decoder.process.pid
        for index in range(250):
            bad = generator.integers(0, 256, int(generator.integers(1, 1200)), np.uint8).tobytes()
            try:
                decoder.decode_packet(bad)
            except PacketError:
                pass
            decoder.discard_pcm()
        assert decoder.process.pid == pid
        decoder.restart()
        decoder.decode_packet(packets(seconds=.1)[0])
        assert decoder.read_pcm()
    finally:
        decoder.close()

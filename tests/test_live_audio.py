import numpy as np
import pytest

from bluehead.audio import Decoder, Mixer
from bluehead.calls import CallRouter
from bluehead.metrics import TrafficMeter
from bluehead.protocol import Format
from .vectors import encode, rtp, signal


@pytest.mark.parametrize("codec", ["aptx", "aptx_hd"])
def test_ongoing_encoder_resumes_mid_parity_cycle_and_recovers_after_packet_loss(codec):
    encoded = b"".join(encode(codec, signal(48000, .8)))
    block = 6 if codec == "aptx_hd" else 4
    raw = [encoded[i:i+block] for i in range(0, len(encoded), block)]
    decoder = Decoder(Format(codec, 48000, 2))
    output = []
    # A real phone can keep its encoder running during a call, then resume
    # at an arbitrary block, rather than emit a fresh encoder's first packet.
    groups = [b"".join(raw[i:i+113]) for i in range(5, len(raw), 113)]
    for sequence, payload in enumerate(groups):
        if sequence == 17:
            continue  # Lose a number of blocks that is not a full parity cycle.
        packet = rtp(payload, sequence) if codec == "aptx_hd" else payload
        output.extend(decoder.feed(packet))
    samples = np.concatenate(output)
    assert decoder.recoveries >= 2
    assert len(samples) > 48000 * .65
    assert np.std(samples[-4800:]) > .02
    assert np.isfinite(samples).all() and np.max(np.abs(samples)) < 1.01
    decoder.close()


@pytest.mark.parametrize("native", [False, True])
def test_per_device_and_master_playback_gains_support_double_amplitude(native):
    mixer = Mixer(buffer_ms=0, native=native)
    mixer.set_gain("phone", 2)
    mixer.add("phone")
    mixer.add("tablet")
    mixer.write("phone", np.full((480, 2), .1, np.float32))
    mixer.write("tablet", np.full((480, 2), .05, np.float32))
    mixer.master = 2
    np.testing.assert_allclose(mixer.render(480), .5, atol=1e-6)
    mixer.set_gain("phone", 0)
    mixer.write("phone", np.full((480, 2), .4, np.float32))
    mixer.write("tablet", np.full((480, 2), .05, np.float32))
    np.testing.assert_allclose(mixer.render(480), .1, atol=1e-6)


def test_uplink_mixes_only_selected_device_and_applies_independent_three_stage_gains():
    router = CallRouter()
    router.configure(16000)
    router.select("phone")
    router.microphone_gain, router.system_gain = 2, .5
    router.device_gains = {"phone": 2, "tablet": 0}
    mic = np.full(120, 1000, "<i2").tobytes()
    computer = np.full(120, 2000, "<i2").tobytes()
    router.write_microphone("tablet", mic)
    router.write_system("tablet", computer)
    assert not any(router.read("phone", 240))
    router.write_microphone("phone", mic)
    router.write_system("phone", computer)
    np.testing.assert_array_equal(np.frombuffer(router.read("phone", 240), "<i2"), 6000)
    router.write_microphone("phone", np.full(120, 30000, "<i2").tobytes())
    assert max(np.frombuffer(router.read("phone", 240), "<i2")) == 32767
    router.write_system("phone", computer)
    router.select("tablet")
    assert not any(router.read("tablet", 240))


def test_live_bitrate_uses_actual_bytes_and_decays_to_zero():
    now = [0.0]
    meter = TrafficMeter(clock=lambda: now[0])
    for step in range(100):
        now[0] = step / 100
        meter.add(720)  # 576 kb/s at one 720-byte payload per 10 ms.
    now[0] = 1
    assert meter.snapshot()["bitrate"] == 576000
    now[0] = 3.1
    assert meter.snapshot() == {"bitrate": 0, "packets_per_second": 0}


def test_ldac_resume_resets_codec_without_restarting_the_isolated_process():
    from bluehead.ldac import LdacDecoder
    decoder = LdacDecoder(48000)
    try:
        pid = decoder.process.pid
        for _ in range(3):
            decoder.reset()
            assert decoder.process.pid == pid and decoder.process.is_alive()
    finally:
        decoder.close()

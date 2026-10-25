"""Real native clock matching, virtual time only; never opens audio/Bluetooth.

PipeWire's controller has three damped poles near 1-beta/3. For the 10ms
renderer/3s period beta=.003333; 10 simulated minutes exceed 60 time constants.
Its unsaturated fixed point is corr=source rate, centred average queue=target, so a
constant +/-500ppm mismatch cannot keep accumulating queue error. The tests
also require real PCM rendering, not a replacement queue model.
"""
import ctypes as C
import json
import math
from pathlib import Path

import numpy as np
import pytest

from bluehead.uplink import CallRouter


def _capture_time(index, ppm, phase=0):
    return index * .01 / (1 + ppm/1000000) + .003 * math.sin(index * 1.113 + phase)


@pytest.mark.parametrize("ppm", [-500, -100, 100, 500])
def test_dual_capture_clocks_30_minutes_no_bulk_drop_or_underrun(ppm, tmp_path):
    router = CallRouter()
    router.configure(48000, adaptive=True)
    router.select("owner")
    mic = (3000*np.sin(2*np.pi*1000*np.arange(480)/48000)).astype("<i2").tobytes()
    system = (2000*np.sin(2*np.pi*2000*np.arange(480)/48000)).astype("<i2").tobytes()
    microphone, computer = 1, 1
    minimum, maximum = [100000, 100000], [0, 0]
    try:
        for cycle in range(240000):  # 1800 s, SCO 7.5ms versus capture 10ms +/-3ms.
            now = cycle * .0075
            while _capture_time(microphone, ppm) <= now:
                router.write_microphone("owner", mic)
                microphone += 1
            while _capture_time(computer, -ppm, .7) <= now:
                router.write_system("owner", system)
                computer += 1
            assert len(router.read("owner", 720)) == 720
            if cycle > 8000 and cycle % 100 == 0:
                for bus in (0, 1):
                    queued = int(router.api.bh_up_size(router.handle, bus)) // 2
                    minimum[bus] = min(minimum[bus], queued)
                    maximum[bus] = max(maximum[bus], queued)
        result = router.snapshot()
        result.update({"ppm": ppm, "seconds": 1800, "jitter_ms": 3, "min_queue": minimum, "max_queue": maximum})
        (tmp_path / f"uplink-{ppm}.json").write_text(json.dumps(result, indent=2), "utf-8")
        for name, expected in (("microphone", ppm), ("system", -ppm)):
            assert result[name]["dropped_frames"] == 0, result
            assert result[name]["underruns"] == 0, result
            assert result[name]["prefill_frames"] <= 2400, result
            assert abs(result[name]["drift_ppm"]-expected) < 50, result
        assert min(minimum) > 200 and max(maximum) < 2500, result
    finally:
        router.close()


def _core():
    api = C.CDLL(str((Path(__file__).parents[1] / "bluehead/assets/bluehead_audio.dll").resolve()))
    ptr, uint = C.c_void_p, C.c_uint32
    for name, arguments, result in (
        ("create", [], ptr), ("free", [ptr], None), ("add", [ptr, uint, uint], C.c_int),
        ("call", [ptr, C.c_int], None), ("write", [ptr, C.c_int, ptr, uint], None),
        ("render", [ptr, ptr, uint, C.c_float], C.c_int), ("value", [ptr, C.c_int, C.c_int], C.c_uint64),
    ):
        function = getattr(api, "bh_mix_" + name)
        function.argtypes, function.restype = arguments, result
    return api


@pytest.mark.parametrize("buffer_ms", [20, 30, 40, 60])
@pytest.mark.parametrize("ppm", [-500, -100, 100, 500])
def test_hfp_downlink_clock_10_minutes_all_buffers_with_3ms_jitter(buffer_ms, ppm, tmp_path):
    api = _core()
    mixer = api.bh_mix_create()
    source = api.bh_mix_add(mixer, buffer_ms*48, 12000)
    api.bh_mix_call(mixer, source)
    # A 360-frame sine changes polarity per packet, preserving phase across 7.5ms.
    wave = .1*np.sin(2*np.pi*1000*np.arange(360)/48000)
    packets = [np.repeat((wave*sign)[:, None], 2, axis=1).astype(np.float32) for sign in (1, -1)]
    output = (C.c_float * 960)()
    packet, minimum, maximum = 1, 100000, 0
    try:
        for cycle in range(60000):  # 600 s; real render 10ms, SCO 7.5ms.
            now = cycle * .01
            while packet*.0075/(1+ppm/1000000)+.003*math.sin(packet*1.113) <= now:
                api.bh_mix_write(mixer, source, packets[(packet-1) % 2].ctypes.data, 360)
                packet += 1
            assert api.bh_mix_render(mixer, output, 480, 1) == 1
            if cycle > 6000 and cycle % 100 == 0:
                queued = int(api.bh_mix_value(mixer, source, 0))
                minimum, maximum = min(minimum, queued), max(maximum, queued)
        values = {name: int(api.bh_mix_value(mixer, source, field)) for field, name in enumerate(
            ("queued", "dropped", "drift", "underruns", "prefill"))}
        values["drift"] -= 1000
        values.update({"ppm": ppm, "buffer_ms": buffer_ms, "seconds": 600, "jitter_ms": 3, "min_queue": minimum, "max_queue": maximum})
        (tmp_path / f"downlink-{buffer_ms}-{ppm}.json").write_text(json.dumps(values, indent=2), "utf-8")
        assert values["dropped"] == values["underruns"] == 0, values
        assert abs(values["drift"]-ppm) < 50, values
        assert 0 < minimum <= maximum < 4800, values
    finally:
        api.bh_mix_free(mixer)


@pytest.mark.parametrize("frequency", [1000, 20000])
@pytest.mark.parametrize("ppm", [-500, 500])
def test_adaptive_tone_frequency_and_level(frequency, ppm):
    router = CallRouter()
    router.configure(48000, adaptive=True)
    router.select("a")
    rate = 48000*(1+ppm/1000000)
    wave = (6000*np.sin(2*np.pi*frequency*np.arange(int(rate*182)+480)/rate)).astype("<i2")
    packet, pieces = 1, []
    try:
        for cycle in range(24000):
            now = cycle*.0075
            while packet*.01/(1+ppm/1000000) <= now:
                begin = (packet-1)*480
                router.write_microphone("a", wave[begin:begin+480].tobytes())
                packet += 1
            data = router.read("a", 720)
            # Average across capture/SCO packet-phase beats rather than treating
            # a three-second phase excursion as a changed tone frequency.
            if cycle >= 20000:
                pieces.append(data)
        signal = np.frombuffer(b"".join(pieces), "<i2").astype(np.float64)
        assert np.sqrt(np.mean(signal**2)) == pytest.approx(6000/np.sqrt(2), rel=.025)
        spectrum = np.abs(np.fft.rfft(signal))
        measured = np.argmax(spectrum)*48000/len(signal)
        assert abs(measured-frequency) < 1, (measured, frequency)
        assert router.snapshot()["microphone"]["underruns"] == 0
    finally:
        router.close()


def test_adaptive_prefill_mute_independent_gain_clear_owner_and_close():
    router = CallRouter()
    router.configure(48000, adaptive=True)
    router.select("a")
    mic = np.full(480, 1000, "<i2").tobytes()
    system = np.full(480, 2000, "<i2").tobytes()
    try:
        router.write_microphone("a", mic)
        assert not any(router.read("a", 720))
        assert router.snapshot()["microphone"]["prefill_frames"] == 360
        # 30ms is the centre-of-render queue target, with half a 7.5ms
        # quantum reserved before rendering; three capture blocks are short.
        for _ in range(3):
            router.write_microphone("a", mic)
        for _ in range(4):
            router.write_system("a", system)
        router.microphone_gain, router.system_gain = 2, .5
        router.device_gains["a"] = 2
        output = np.frombuffer(router.read("a", 720), "<i2")
        np.testing.assert_allclose(output, 6000, atol=1)
        queued = router.snapshot()["microphone"]["queued_frames"]
        assert not any(router.read("b", 720))
        assert router.snapshot()["microphone"]["queued_frames"] == queued
        router.clear_system()
        assert router.snapshot()["system"]["queued_frames"] == 0
        router.muted = True
        for _ in range(80):
            router.write_microphone("a", mic)
            assert not any(router.read("a", 960))
        assert router.snapshot()["microphone"]["dropped_frames"] == 0
        router.muted = False
        for _ in range(3):
            router.write_microphone("a", mic)
        assert any(router.read("a", 720))
        router.select("b")
        router.write_microphone("a", mic)
        assert not any(router.read("b", 720))
        with pytest.raises(ValueError):
            router.write_microphone("b", b"x")
        with pytest.raises(ValueError):
            router.configure(16000, adaptive=True)
    finally:
        router.close()
        router.close()


def test_legacy_low_rate_fifo_still_has_no_adaptive_startup_delay():
    router = CallRouter()
    try:
        router.configure(16000)
        router.select("legacy")
        router.write_microphone("legacy", b"\xe8\x03"*120)
        assert router.read("legacy", 240) == b"\xe8\x03"*120
        assert not router.snapshot()["adaptive"]
    finally:
        router.close()

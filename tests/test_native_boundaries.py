"""Independent native audio boundary, lifecycle and clock-domain regressions.

No physical devices. The drift harness drives the real native uplink with a
10ms capture clock and a separate 7.5ms SCO clock without wall-clock sleeping.
"""

from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

from bluehead.pcm import NativeMixer
from bluehead.uplink import CallRouter
from .offline_audio import OfflineEndpoints

ROOT = Path(__file__).resolve().parents[1]


def uplink_clock_diagnostics(ppm, seconds=1800, *, jitter_us=0):
    router = CallRouter()
    router.select("virtual-headset")
    router.configure(48000, adaptive=True)
    router.write_microphone("virtual-headset", b"\x11\x11" * 1440)
    block = b"\x11\x11" * 480
    capture, reads = 1, 0
    trims, maximum, previous_drop = [], 0, 0
    try:
        while reads < round(seconds / 0.0075):
            capture_time = capture * 10000 / (1 + ppm / 1000000)
            # The producer still generates exactly 480 samples per event;
            # deterministic +/- jitter changes arrival time, never audio rate.
            if jitter_us:
                capture_time += (capture % 3 - 1) * jitter_us
            if capture_time <= reads * 7500:
                router.write_microphone("virtual-headset", block)
                after = router.api.bh_up_size(router.handle, 0)
                dropped = int(router.api.bh_up_value(router.handle, 0, 1))
                if dropped > previous_drop:
                    trims.append((capture_time / 1e6, dropped - previous_drop))
                previous_drop = dropped
                maximum = max(maximum, after // 2)
                capture += 1
            else:
                output = router.read("virtual-headset", 720)
                assert len(output) == 720
                reads += 1
        return {
            "ppm": ppm,
            "seconds": seconds,
            "trim_events": len(trims),
            "first_trims": trims[:4],
            "max_trim_samples": max((size for _, size in trims), default=0),
            # An SRC may consume 359, 360 or 361 frames for a 360-frame read.
            # Native counters distinguish that normal correction from a gap.
            "underflow_events": int(router.api.bh_up_value(router.handle, 0, 3)),
            "prefill_frames": int(router.api.bh_up_value(router.handle, 0, 4)),
            "max_queue_samples": maximum,
            "final_queue_samples": router.api.bh_up_size(router.handle, 0) // 2,
        }
    finally:
        router.close()


@pytest.mark.parametrize("ppm", [-500, 0, 500])
def test_capture_and_sco_clocks_do_not_require_periodic_bulk_trim_or_silence(ppm):
    result = uplink_clock_diagnostics(ppm)
    assert result["max_queue_samples"] <= 4320, result
    assert result["max_trim_samples"] == 0, result
    assert result["underflow_events"] == 0, result


def test_10ms_capture_bursts_and_7p5ms_sco_survive_worker_jitter():
    result = uplink_clock_diagnostics(0, seconds=120, jitter_us=2500)
    assert not result["trim_events"] and not result["underflow_events"], result


def test_capture_thread_start_failure_can_close_native_endpoint(monkeypatch):
    import bluehead.capture as capture

    endpoints = OfflineEndpoints(monkeypatch)
    stream = capture.CaptureStream(0, 48000, lambda data: None, lambda *args, **kwargs: None)
    original = threading.Thread

    class CannotStart(original):
        def start(self):
            raise RuntimeError("thread resources exhausted")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(capture.threading, "Thread", CannotStart)
            with pytest.raises(RuntimeError, match="resources exhausted"):
                stream.start()
        stream.close()
        assert stream.closed and stream.native.closed and not endpoints.owner._streams
    finally:
        # Leave no leaked handle even if the regression still exists.
        if stream.worker is not None and stream.worker.ident is None:
            stream.worker = None
        stream.close()
        endpoints.close()


def test_dead_capture_worker_is_detected_and_can_be_replaced_without_inventory_change(monkeypatch):
    from types import SimpleNamespace
    from bluehead.capture import CaptureStream
    import bluehead.audio_runtime as runtime

    endpoints = OfflineEndpoints(monkeypatch)
    events, delivered = [], []

    def fail(data):
        raise RuntimeError("consumer temporarily unavailable")

    stream = CaptureStream(0, 48000, fail, lambda *args, **kwargs: events.append((args, kwargs)))
    monkeypatch.setattr(runtime, "_instances", [SimpleNamespace(closed=False, running=True, stream=stream)])
    try:
        stream.start()
        endpoints.process(stream.native, input=np.full(480, 0.2, np.float32))
        stream.worker.join(2)
        assert not stream.worker.is_alive() and events
        assert runtime.streams_unhealthy()
        stream.stop()
        stream.consume = delivered.append
        stream.start()
        endpoints.process(stream.native, input=np.full(480, 0.3, np.float32))
        deadline = time.monotonic() + 2
        while not delivered and time.monotonic() < deadline:
            time.sleep(0.002)
        assert delivered and not runtime.streams_unhealthy()
    finally:
        stream.close()
        endpoints.close()


def test_spsc_clear_remove_reuse_is_finite_and_bounded_under_real_concurrency():
    mixer = NativeMixer()
    api, handle = mixer.library, mixer.handle
    index = api.bh_mix_add(handle, 0, 4096)
    assert index == 0
    failures = []
    source = np.full((127, 2), 0.125, np.float32)
    output = np.empty((113, 2), np.float32)
    start = threading.Barrier(3)

    def produce():
        try:
            start.wait()
            for _ in range(3000):
                api.bh_mix_write(handle, index, source.ctypes.data, len(source))
        except BaseException as error:
            failures.append(error)

    def render():
        try:
            start.wait()
            for _ in range(3000):
                assert api.bh_mix_render(handle, output.ctypes.data, len(output), 1) >= 0
                assert np.isfinite(output).all()
                assert np.max(np.abs(output)) <= 0.126
        except BaseException as error:
            failures.append(error)

    workers = [threading.Thread(target=produce), threading.Thread(target=render)]
    for worker in workers:
        worker.start()
    start.wait()
    try:
        for turn in range(1000):
            if turn % 5:
                api.bh_mix_clear(handle, index)
            else:
                api.bh_mix_remove(handle, index)
                assert api.bh_mix_add(handle, 0, 4096) == index
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
        assert not failures
        assert api.bh_mix_value(handle, index, 0) <= 4096
    finally:
        for worker in workers:
            worker.join(5)
        mixer.close()


def test_producer_critical_section_cannot_silence_or_block_native_renderer():
    # Isolate this ABI-specific lock-contention probe. Win64 CRITICAL_SECTION
    # is 40 bytes; Mixer starts with lifecycle, then input[0].producer. The
    # production callback must consume already-published ring data while that
    # producer lock is held. No audio device or OS callback is opened.
    program = """
import ctypes as c, threading
import numpy as np
from bluehead.pcm import NativeMixer
assert c.sizeof(c.c_void_p)==8
m=NativeMixer(); s=m.add(0,4096); assert s.index==0
s.write(np.full((2048,2),.125,np.float32))
kernel=c.WinDLL('kernel32',use_last_error=True)
for name in ('EnterCriticalSection','LeaveCriticalSection'):
    getattr(kernel,name).argtypes=[c.c_void_p]
lock=m.handle+40
done=threading.Event(); out=np.empty((480,2),np.float32)
def render():
    m.render_into(out,1); done.set()
kernel.EnterCriticalSection(lock)
t=threading.Thread(target=render)
try:
    t.start()
    assert done.wait(.25), 'Render blocked on producer lock'
    np.testing.assert_array_equal(out,.125)
finally:
    kernel.LeaveCriticalSection(lock); t.join(2); m.close()
print('lock-free render verified')
"""
    result = subprocess.run([sys.executable, "-c", program], cwd=ROOT, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and "lock-free render verified" in result.stdout, result.stderr


def test_malformed_rtp_and_aptx_parity_fuzz_is_bounded_and_can_recover():
    # A native decoder fault/hang fails this bounded child rather than killing
    # the whole validation run. All input is synthetic and no audio is played.
    program = """
import ctypes as c,json,random
from bluehead.media import NativeMedia
from bluehead.protocol import Format,PacketError
rng=random.Random(9876123); result={}
for codec in ('sbc','aac','aptx','aptx_hd'):
    decoder=NativeMedia(Format(codec,48000,2)); total=0
    try:
        for index in range(800):
            size=rng.randrange(1,600)
            data=rng.randbytes(size)
            if index%3==0 and codec!='aptx':
                data=bytes([0x80,96])+index.to_bytes(2,'big')+bytes(8)+data
            try:decoder.decode_packet(data)
            except PacketError:pass
            assert decoder.value(7)<=48064
            output=(c.c_float*(2*64+8))(*([123.0]*(2*64+8)))
            count=decoder.api.bh_media_read(decoder.handle,output,64)
            assert count<=64
            assert list(output[128:])==[123.0]*8
            assert all(abs(x)<=3.5e38 for x in output[:count*2])
            total+=count
        decoder.restart();decoder.discard_pcm()
        assert decoder.value(7)==0
        result[codec]={'frames_read':total,'errors':decoder.errors,'malformed':decoder.value(9)}
    finally:decoder.close()
print(json.dumps(result))
"""
    result = subprocess.run([sys.executable, "-c", program], cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr[-3000:]
    counts = json.loads(result.stdout)
    assert set(counts) == {"sbc", "aac", "aptx", "aptx_hd"}


@pytest.mark.parametrize("codec", ["sbc", "aac", "aptx_hd"])
def test_rtp_extension_padding_and_fragment_lengths_are_rejected_before_decode(codec):
    from bluehead.media import NativeMedia
    from bluehead.protocol import Format, PacketError

    decoder = NativeMedia(Format(codec, 48000, 2))
    try:
        packets = [
            b"\x80",
            b"\x80" + bytes(10),
            b"\x8f" + bytes(11),
            b"\x90" + bytes(11) + b"\x00\x00\xff\xff",
            b"\xa0" + bytes(11) + b"\x00",
            b"\xa0" + bytes(11) + b"\xff",
        ]
        for packet in packets:
            with pytest.raises(PacketError):
                decoder.decode_packet(packet)
        assert decoder.value(9) == len(packets)
        assert decoder.value(7) == 0
    finally:
        decoder.close()


if __name__ == "__main__":
    # Reproduce exact real-FIFO drift observations without running pytest.
    print(json.dumps([uplink_clock_diagnostics(ppm) for ppm in (-500, 0, 500)], indent=2))

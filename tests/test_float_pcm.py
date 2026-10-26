"""Signal checks for the production float bridge and legacy S16 adapters."""
import ctypes as C

import numpy as np
import pytest

from .test_uplink_clock import _core
from .test_native_voice import Voice, dll  # noqa: F401  pytest fixture


def audio_api():
    api = _core()
    api.bh_capture_create.argtypes = [C.c_uint32] * 3
    api.bh_capture_create.restype = C.c_void_p
    api.bh_capture_free.argtypes = [C.c_void_p]
    api.bh_capture_push.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32]
    api.bh_capture_read.argtypes = api.bh_capture_read_f32.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32]
    api.bh_up_write_f32.argtypes = [C.c_void_p, C.c_int, C.c_void_p, C.c_uint32]
    api.bh_up_read_f32.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32]
    api.bh_up_create.restype = C.c_void_p
    api.bh_up_free.argtypes = [C.c_void_p]
    api.bh_up_rate.argtypes = [C.c_void_p, C.c_uint32]
    api.bh_up_adaptive.argtypes = [C.c_void_p, C.c_int]
    return api


def capture(api, rate, floating, sizes):
    handle = api.bh_capture_create(rate, 1, 48000)
    assert handle
    count = rate // 8
    pcm = (.35/32768*np.sin(np.arange(count)*2*np.pi*997/rate)).astype('f4')
    result = []
    output = np.empty(1024, 'f4' if floating else 'i2')
    read = api.bh_capture_read_f32 if floating else api.bh_capture_read
    offset = step = 0
    try:
        while offset < count:
            take = min(sizes[step % len(sizes)], count-offset)
            assert api.bh_capture_push(handle, pcm[offset:].ctypes.data, take) == take
            offset += take
            step += 1
            while (got := read(handle, output.ctypes.data, len(output))) > 0:
                result.append(output[:got].copy())
            assert got == 0
        return np.concatenate(result)
    finally:
        api.bh_capture_free(handle)


@pytest.mark.parametrize('rate', [44100, 48000, 96000, 192000])
def test_capture_float_preserves_sub_s16_detail_without_chunk_dependence(rate):
    api = audio_api()
    fixed = capture(api, rate, True, (rate//100,))
    irregular = capture(api, rate, True, (1, 17, 113, 256, 511, 960))
    np.testing.assert_allclose(fixed, irregular, atol=2e-12)
    actual = fixed[len(fixed)//2:]
    assert np.sqrt(np.mean(actual**2)) == pytest.approx(.35/32768/np.sqrt(2), rel=.015)
    legacy = capture(api, rate, False, (rate//100,))
    assert not legacy.any()  # The former bridge irreversibly rounded this to zero.


def test_uplink_float_keeps_low_level_mix_until_final_codec_boundary():
    api = audio_api()
    handle = api.bh_up_create()
    assert handle
    try:
        assert api.bh_up_rate(handle, 48000) == 0
        assert api.bh_up_adaptive(handle, 1) == 0
        quiet = (.35/32768*np.sin(np.arange(2880)*2*np.pi*997/48000)).astype('f4')
        assert api.bh_up_write_f32(handle, 0, quiet.ctypes.data, len(quiet)) == len(quiet)
        output = np.empty(1440, 'f4')
        assert api.bh_up_read_f32(handle, output.ctypes.data, len(output)) == len(output)
        assert np.sqrt(np.mean(output[480:]**2)) > .2/32768
        assert np.max(abs(output)) < .5/32768
    finally:
        api.bh_up_free(handle)


@pytest.mark.parametrize('rate', [44100, 48000, 96000, 192000])
def test_capture_clock_uses_producer_progress_not_consumer_drain_time(rate):
    api = audio_api()
    api.bh_capture_clock.argtypes = [C.c_void_p, C.POINTER(C.c_uint64), C.POINTER(C.c_uint64)]
    handle = api.bh_capture_create(rate, 1, 48000)
    assert handle
    position, arrival = C.c_uint64(), C.c_uint64()
    try:
        assert api.bh_capture_clock(handle, C.byref(position), C.byref(arrival)) == 0
        assert api.bh_capture_push(handle, None, rate//100) == rate//100
        assert api.bh_capture_clock(handle, C.byref(position), C.byref(arrival)) == 1
        assert position.value == 480 and arrival.value > 0
        stamp = arrival.value
        output = np.empty(1024, 'f4')
        assert api.bh_capture_read_f32(handle, output.ctypes.data, len(output)) > 0
        assert api.bh_capture_clock(handle, C.byref(position), C.byref(arrival)) == 1
        assert position.value == 480 and arrival.value == stamp
        # Even discarded input occupies time; a full queue cannot stop its clock.
        for _ in range(10):
            api.bh_capture_push(handle, None, rate//100)
        assert api.bh_capture_clock(handle, C.byref(position), C.byref(arrival)) == 1
        assert position.value == 5280 and arrival.value >= stamp
    finally:
        api.bh_capture_free(handle)


@pytest.mark.parametrize('codec', [1, 2, 3])
def test_voice_float_decode_is_unquantized_and_s16_adapter_is_consistent(dll, codec):  # noqa: F811 imported pytest fixture
    dll.bh_voice_encode_f32.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32]
    dll.bh_voice_read_f32.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32]
    dll.bh_voice_read_f32.restype = C.c_uint32
    sender, floating, integer = (Voice(dll, codec) for _ in range(3))
    pieces = []
    try:
        for frame in range(40):
            pcm = (.17*np.sin(np.arange(frame*360, (frame+1)*360)*2*np.pi*997/48000)).astype('f4')
            wire = C.create_string_buffer(120)
            size = dll.bh_voice_encode_f32(sender.handle, pcm.ctypes.data, 360, wire, 120)
            assert size == (120 if codec == 1 else 60)
            packet = wire.raw[:size]
            floating.push(packet)
            integer.push(packet)
            out = np.empty(360, 'f4')
            assert dll.bh_voice_read_f32(floating.handle, out.ctypes.data, 360) == 360
            converted = np.rint(np.clip(out*32768, -32768, 32767)).astype('i2')
            np.testing.assert_array_equal(integer.read(), converted)
            pieces.append(out)
        signal = np.concatenate(pieces)[3600:]
        assert .09 < np.sqrt(np.mean(signal**2)) < .14
        assert np.mean(abs(signal*32768-np.rint(signal*32768))) > .1
    finally:
        for voice in (sender, floating, integer):
            voice.close()

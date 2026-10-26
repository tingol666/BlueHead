"""Actual C allocation balance and owner/route exception-unwind ordering."""
import ctypes as C

import numpy as np

from tools.audio_fidelity_audit import build_probe, mixer_api


def test_prepared_and_active_routes_keep_storage_until_last_lease_then_free_every_allocation():
    api = mixer_api(build_probe())
    api.bh_mix_route_create.argtypes = [C.c_void_p, C.c_uint]
    api.bh_mix_route_create.restype = C.c_void_p
    api.bh_mix_route_free.argtypes = [C.c_void_p]
    api.bh_mix_render_route.argtypes = [C.c_void_p, C.c_void_p, C.c_uint, C.c_float]
    api.bh_test_live_allocations.restype = C.c_longlong
    baseline = api.bh_test_live_allocations()
    for _ in range(10):
        mixer = api.bh_mix_create()
        first, candidate = api.bh_mix_route_create(mixer, 48000), api.bh_mix_route_create(mixer, 96000)
        assert first and candidate
        api.bh_mix_free(mixer)  # Emulate exception unwinding closing the owner first.
        api.bh_mix_free(mixer)  # Repeated close is harmless while a route still owns it.
        assert not api.bh_mix_route_create(mixer, 44100)
        output = np.ones((480, 2), 'f4')
        assert api.bh_mix_render_route(first, output.ctypes.data, len(output), 1) == 0
        assert not output.any()
        assert api.bh_test_live_allocations() > baseline
        api.bh_mix_route_free(candidate)
        api.bh_mix_route_free(first)
        assert api.bh_test_live_allocations() == baseline


def test_mixer_constructor_and_format_allocation_failures_keep_old_audio_and_release_temporaries():
    api = mixer_api(build_probe())
    api.bh_test_fail_allocation_after.argtypes = [C.c_int]
    api.bh_test_live_allocations.restype = C.c_longlong
    api.bh_mix_format.argtypes = [C.c_void_p, C.c_int, C.c_uint]
    api.bh_mix_value.argtypes = [C.c_void_p, C.c_int, C.c_int]
    api.bh_mix_value.restype = C.c_uint64
    api.bh_mix_route_create.argtypes = [C.c_void_p, C.c_uint]
    api.bh_mix_route_create.restype = C.c_void_p
    api.bh_mix_route_free.argtypes = [C.c_void_p]
    baseline = api.bh_test_live_allocations()
    try:
        for budget in range(15):
            api.bh_test_fail_allocation_after(budget)
            mixer = api.bh_mix_create()
            if mixer:
                api.bh_mix_free(mixer)
            assert api.bh_test_live_allocations() == baseline
        api.bh_test_fail_allocation_after(-1)
        mixer = api.bh_mix_create()
        routes = [api.bh_mix_route_create(mixer, rate) for rate in (48000, 96000)]
        source = api.bh_mix_add(mixer, 0, 48000)
        pcm = np.full((800, 2), .125, 'f4')
        api.bh_mix_write(mixer, source, pcm.ctypes.data, len(pcm))
        live = api.bh_test_live_allocations()
        try:
            for budget in range(8):
                api.bh_test_fail_allocation_after(budget)
                assert api.bh_mix_format(mixer, source, 192000) == -1
                assert api.bh_mix_value(mixer, source, 20) == 48000
                assert api.bh_mix_value(mixer, source, 0) == 800
                assert api.bh_test_live_allocations() == live
            api.bh_test_fail_allocation_after(-1)
            assert api.bh_mix_format(mixer, source, 192000) == 0
            assert api.bh_mix_value(mixer, source, 20) == 192000
        finally:
            api.bh_mix_free(mixer)
            for route in routes:
                api.bh_mix_route_free(route)
        assert api.bh_test_live_allocations() == baseline
    finally:
        api.bh_test_fail_allocation_after(-1)

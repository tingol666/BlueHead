"""Signal contracts for the actual production C mixer, independent of hardware.

Use mixed spectra and transients as well as tones. Buffer/packet-loss tests
cannot establish transparency, callback independence or source isolation.
"""
import ctypes as C
from types import SimpleNamespace

import numpy as np
import pytest

from .test_continuity import core
from bluehead.realtime import NativeRender


def setup():
    api = core()
    for name, args in (
        ("gain", [C.c_void_p, C.c_int, C.c_float]),
        ("clear", [C.c_void_p, C.c_int]),
        ("remove", [C.c_void_p, C.c_int]),
    ):
        getattr(api, "bh_mix_" + name).argtypes = args
    mixer = api.bh_mix_create()
    return api, mixer


def add(api, mixer, pcm):
    source = api.bh_mix_add(mixer, 0, 48000)
    assert api.bh_mix_continuity(mixer, source, 1, 0, 0) == 0
    data = np.ascontiguousarray(pcm, dtype='f4')
    api.bh_mix_write(mixer, source, data.ctypes.data, len(data))
    return source


def take(api, mixer, count, master=1):
    out = np.empty((count, 2), 'f4')
    assert api.bh_mix_render(mixer, out.ctypes.data, count, master) == 1
    assert np.isfinite(out).all()
    return out


def process(pcm, sizes, master=1):
    api, mixer = setup()
    try:
        add(api, mixer, pcm)
        result, offset, block = [], 0, 0
        while offset < len(pcm):
            count = min(sizes[block % len(sizes)], len(pcm) - offset)
            result.append(take(api, mixer, count, master))
            offset += count
            block += 1
        return np.concatenate(result)
    finally:
        api.bh_mix_free(mixer)


def music(count, scale=1):
    t = np.arange(count) / 48000
    signal = .24 * np.sin(2*np.pi*97*t) + .17 * np.sin(2*np.pi*1733*t)
    signal += .09 * np.sin(2*np.pi*13711*t)
    return np.column_stack((signal, -.37*signal)).astype('f4') * scale


def test_overload_limiter_is_independent_of_callback_partition_and_stereo_linked():
    pcm = music(12000, 3)
    pcm[2000, :] = [3.5, -1.295]  # A transient must not normalize its entire callback.
    fixed = process(pcm, (480,))
    irregular = process(pcm, (1, 17, 113, 256, 511, 960))
    np.testing.assert_allclose(fixed, irregular, atol=3e-7)
    np.testing.assert_allclose(fixed[:, 1], -.37*fixed[:, 0], atol=3e-7)
    assert abs(fixed).max() <= 1


def test_unity_path_preserves_full_spectrum_and_transients_after_fixed_delay():
    pcm = music(12000)
    pcm[3000, :] = [.995, -.36815]
    output = process(pcm, (1, 127, 480, 1024))
    # Only the explicitly bounded limiter lookahead is permitted; no temporal
    # stretching or content-dependent delay on the no-loss, unity-clock path.
    delay = int(np.argmax(abs(output[2990:3120, 0]))) + 2990 - 3000
    assert 0 <= delay <= 96
    actual = output[1000+delay:11000+delay]
    np.testing.assert_allclose(actual, pcm[1000:11000], atol=2e-7)


@pytest.mark.parametrize('bus', ['source', 'master', 'duck'])
def test_live_gain_changes_are_short_continuous_ramps_and_true_mute_is_immediate(bus):
    api, mixer = setup()
    try:
        source = add(api, mixer, np.full((5000, 2), .2, 'f4'))
        before = take(api, mixer, 1000)
        if bus == 'source':
            api.bh_mix_gain(mixer, source, 2)
        elif bus == 'duck':
            api.bh_mix_call(mixer, source + 1)
        after = take(api, mixer, 1000, 2 if bus == 'master' else 1)
        combined = np.r_[before[-1, 0], after[:, 0]]
        assert abs(np.diff(combined)).max() < .003
        target = .03 if bus == 'duck' else .4
        assert after[-1, 0] == pytest.approx(target, abs=1e-6)
        if bus == 'source':
            api.bh_mix_gain(mixer, source, 0)
            assert not take(api, mixer, 480).any()
        else:
            assert not take(api, mixer, 480, 0).any()
    finally:
        api.bh_mix_free(mixer)


def test_source_clear_and_slot_reuse_cannot_leak_delayed_pcm_or_drop_another_source():
    api, mixer = setup()
    try:
        source = add(api, mixer, np.full((5000, 2), .25, 'f4'))
        survivor = add(api, mixer, np.full((5000, 2), .1, 'f4'))
        take(api, mixer, 1000)
        api.bh_mix_clear(mixer, source)
        after = take(api, mixer, 480)
        np.testing.assert_allclose(after, .1, atol=1e-7)
        api.bh_mix_remove(mixer, source)
        replacement = add(api, mixer, np.zeros((5000, 2), 'f4'))
        assert replacement == source and survivor != source
        np.testing.assert_allclose(take(api, mixer, 480), .1, atol=1e-7)
    finally:
        api.bh_mix_free(mixer)


def test_limiter_returns_to_transparent_unity_after_overload():
    api, mixer = setup()
    try:
        pcm = np.full((480, 2), .1, 'f4')
        transient = pcm.copy()
        transient[200] = 3.5
        source = add(api, mixer, transient)
        take(api, mixer, 480)
        for _ in range(200):
            api.bh_mix_write(mixer, source, pcm.ctypes.data, 480)
            output = take(api, mixer, 480)
        np.testing.assert_allclose(output, .1, atol=2e-7)
        before = api.bh_mix_value(mixer, -1, 2)
        api.bh_mix_write(mixer, source, pcm.ctypes.data, 480)
        np.testing.assert_allclose(take(api, mixer, 480), .1, atol=2e-7)
        assert api.bh_mix_value(mixer, -1, 2) == before
    finally:
        api.bh_mix_free(mixer)


def test_dc_biased_uncorrelated_stereo_does_not_masquerade_as_periodic_audio():
    api, mixer = setup()
    rng = np.random.default_rng(147)
    try:
        # DC makes raw correlation look high; right-channel unpitched detail
        # must not be repeated as a synthetic tonal buzz through a 20ms gap.
        pcm = music(2400)
        pcm[:, 0] += .25
        pcm[:, 1] = .3 + rng.normal(0, .08, len(pcm))
        add(api, mixer, pcm)
        take(api, mixer, 2400)
        gap = take(api, mixer, 720)
        assert np.max(abs(gap[240:])) < 1e-7
    finally:
        api.bh_mix_free(mixer)


@pytest.mark.parametrize('rate', [44100, 48000, 96000, 192000])
def test_production_format_conversion_preserves_mixed_spectrum_across_arbitrary_chunks(rate):
    pcm = music(24000)

    def run(sizes):
        api, mixer = setup()
        renderer = None
        try:
            add(api, mixer, pcm)
            renderer = NativeRender(SimpleNamespace(handle=mixer), rate)
            pieces, count = [], 0
            while count < rate // 3:
                n = min(sizes[len(pieces) % len(sizes)], rate // 3 - count)
                out = np.empty((n, 2), 'f4')
                renderer.render_into(out)
                pieces.append(out)
                count += n
            return np.concatenate(pieces)
        finally:
            if renderer:
                renderer.close()
            api.bh_mix_free(mixer)

    fixed = run((rate // 100,))
    irregular = run((1, 17, 113, 256, 511, 960))
    np.testing.assert_allclose(fixed, irregular, atol=3e-7)
    np.testing.assert_allclose(fixed[:, 1], -.37*fixed[:, 0], atol=3e-7)


@pytest.mark.parametrize('rate', [44100, 96000])
def test_fallback_mute_covers_pending_conversion_input_and_filter_tail(rate):
    api, mixer = setup()
    renderer = None
    try:
        add(api, mixer, np.full((10000, 2), .2, 'f4'))
        renderer = NativeRender(SimpleNamespace(handle=mixer), rate)
        renderer.render_into(np.empty((rate // 20, 2), 'f4'))
        api.bh_mix_master.argtypes = [C.c_void_p, C.c_float]
        api.bh_mix_master(mixer, 0)
        output = np.empty((17, 2), 'f4')  # Leaves pending conversion input.
        renderer.render_into(output)
        assert not output.any()
        api.bh_mix_master(mixer, 1)
        renderer.render_into(output)
        assert abs(output).max() < .03  # Smooth restart, no old full-level tail.
    finally:
        if renderer:
            renderer.close()
        api.bh_mix_free(mixer)

"""Actual native recovery policy and route/codec transactions, without hardware."""
from tools.native_build import compiler as find_compiler
import ctypes as C
import struct
import time
from pathlib import Path
import subprocess
from types import SimpleNamespace
from io import StringIO
import asyncio

import numpy as np
import pytest

from bluehead.errors import NativeAudioError, SourceExpired, Retirement
from bluehead.errors import AudioHealth
from bluehead.native_runtime import NativeClient, RemoteMixer, RemoteMediaSession, NativeTransport
from bluehead.protocol import Format
from bluehead.backend import Receiver, Settings
from bluehead.engine import Engine
from bluehead.native_calls import NativeCallManager
from .test_audio_timeline import timeline_api, signal
from .test_continuity import render
from .test_native_runtime import source, packet, wait_done
from .test_native_voice import Voice, dll, tone  # noqa: F401


def test_retry_delay_grows_and_only_five_healthy_seconds_clear_history():
    events = [
        dict(time=0, code=-204), dict(time=.49, action='tick'),
        dict(time=.5, action='tick'), dict(time=.5, code=-204),
        dict(time=1.49, action='tick'), dict(time=1.5, action='tick'),
        dict(time=1.6, action='success'), dict(time=6.5, action='tick'),
        dict(time=6.61, action='tick'),
    ]
    with NativeClient(spawn=True) as client:
        values = client.request('test_recovery_policy', events=events)
    assert values[0]['category'] == 'audio_endpoint'
    assert [values[i]['ready'] for i in (1, 2, 4, 5)] == [False, True, False, True]
    assert values[6]['state'] == values[7]['state'] == 'recovering'
    assert values[7]['attempts'] == 2
    assert values[8]['state'] == 'healthy' and values[8]['attempts'] == 0
    assert values[8]['failures'] == 2 and values[8]['recoveries'] == 1


@pytest.mark.parametrize('code,category,retry_ms', [
    (-6, 'audio_permission', 0), (-200, 'audio_format', 0),
    (-19, 'audio_busy', 500), (-4, 'audio_memory', 10000),
    (-34, 'audio_timeout', 500), (-204, 'audio_endpoint', 500),
])
def test_fault_categories_do_not_share_one_reset_or_retry_policy(code, category, retry_ms):
    with NativeClient(spawn=True) as client:
        values = client.request('test_recovery_policy', events=[
            dict(time=1, code=code), dict(time=2, action='manual')])
    assert values[0]['category'] == category
    assert values[0]['retry_in_ms'] == retry_ms
    assert values[0]['automatic'] == bool(retry_ms)
    assert values[1]['state'] == 'healthy' and values[1]['ready']


def test_unknown_failure_cannot_cause_an_endless_fast_restart_storm():
    with NativeClient(spawn=True) as client:
        result = client.request('test_recovery_policy', events=[
            dict(time=1, code=-1), dict(time=2, code=-1), dict(time=4, code=-1)])
    assert result[-1]['state'] == 'needs_attention'
    assert not result[-1]['automatic'] and not result[-1]['ready']


def test_malformed_packet_is_not_a_reason_to_rebuild_decoder_history():
    with NativeClient(spawn=True) as client:
        events = [dict(time=i*.01, action='decode', failed=True, malformed=True) for i in range(30)]
        events += [dict(time=1+i*.01, action='decode', failed=True) for i in range(8)]
        events += [dict(time=1.1+i*.01, action='decode', failed=True) for i in range(8)]
        values = client.request('test_recovery_policy', events=events)
    assert not any(v['rebuild'] for v in values[:30])
    assert sum(v['rebuild'] for v in values) == 1


@pytest.mark.parametrize('direction', ['input', 'output'])
@pytest.mark.parametrize('code,category', [(-204, 'audio_endpoint'), (-6, 'audio_permission'), (-200, 'audio_format')])
def test_failed_route_candidate_keeps_previous_route_and_reports_structured_cause(direction, code, category):
    with NativeClient(spawn=True) as client:
        client.wait_audio(client.request('test_audio_switch', direction=direction, endpoint_id='good'))
        ticket = client.request('test_audio_switch', direction=direction, endpoint_id='bad', error_code=code)
        with pytest.raises(NativeAudioError) as caught:
            client.wait_audio(ticket)
        assert caught.value.category == category and caught.value.native_code == code
        assert '当前设备继续使用' in str(caught.value)
        assert not caught.value.retryable
        snapshot = client.request('snapshot')
        assert snapshot['microphone_active' if direction == 'input' else 'output_active']
        client.wait_audio(client.request('test_audio_switch', direction=direction, endpoint_id='new-good'))
        recovery = client.request('snapshot')['devices']['recovery']['microphone' if direction == 'input' else 'output']
        assert recovery['state'] == 'healthy'


def test_cancelled_prepare_failure_cannot_poison_the_new_route_recovery_state():
    with NativeClient(spawn=True) as client:
        old = client.request('test_audio_switch', direction='input', delay_ms=100, error_code=-6)
        new = client.request('test_audio_switch', direction='input')
        client.wait_audio(new)
        assert client.request('audio_result', ticket=old)['cancelled']
        state = client.request('snapshot')['devices']['recovery']['microphone']
        assert state['state'] == 'healthy' and state['failures'] == 0


def test_stalled_media_worker_discards_old_compressed_audio_before_decoding_it():
    with NativeClient(spawn=True) as client:
        client.request('test_replay', seconds=3.5, codec='aac')
        time.sleep(.8)
        client.request('test_stall', direction='media', milliseconds=500)
        report = wait_done(client)
        state = client.request('snapshot')['sources']['offline:media']
        assert state['media']['expired_packets'] > 5
        assert state['media']['decoder_restarts'] >= 1
        assert report['codec_errors'] == 0
        assert state['queued_frames'] < 8640
        assert state['callback_period_searches'] == 0


@pytest.mark.parametrize('codec', [1, 2, 3])
def test_receive_recovery_does_not_reset_transmit_codec_state_or_h2_sequence(dll, codec):  # noqa: F811 imported pytest fixture
    dll.bh_voice_reset_rx.argtypes = [C.c_void_p]
    duplex, control = Voice(dll, codec), Voice(dll, codec)
    try:
        for i in range(20):
            if i == 7:
                assert dll.bh_voice_reset_rx(duplex.handle) == 0
                assert len(duplex.read()) == 0
            expected = control.encode(tone(i))
            actual = duplex.encode(tone(i))
            assert actual == expected
            duplex.push(actual)
            assert len(duplex.read()) == 360
        assert duplex.stats().tx_frames == control.stats().tx_frames == 20
    finally:
        duplex.close()
        control.close()


def test_nonfinite_pcm_is_removed_before_it_can_poison_interpolation_history():
    api = timeline_api()
    original = signal(0, 6000)
    damaged = original.copy()
    damaged[200, 0], damaged[700, 1], damaged[1300, 0] = np.nan, np.inf, -np.inf
    damaged[1500, 1] = 1e30
    clean = np.nan_to_num(damaged, nan=0, posinf=0, neginf=0)
    clean[1500, 1] = 0
    outputs = []
    for data in (damaged, clean):
        mixer = api.bh_mix_create()
        source = api.bh_mix_add(mixer, 480, 8640)
        api.bh_mix_continuity(mixer, source, 1, 480, 480)
        api.bh_mix_timeline(mixer, source, 1)
        try:
            api.bh_mix_write(mixer, source, data.ctypes.data, len(data))
            outputs.append(render(api, mixer, 1920))
            assert api.bh_mix_value(mixer, source, 19) == (4 if data is damaged else 0)
        finally:
            api.bh_mix_free(mixer)
    np.testing.assert_array_equal(*outputs)


def test_pcm_overrun_recovers_to_fresh_target_without_replaying_full_maximum_backlog():
    api = timeline_api()
    mixer = api.bh_mix_create()
    source = api.bh_mix_add(mixer, 480, 4320)
    api.bh_mix_continuity(mixer, source, 1, 480, 960)
    api.bh_mix_timeline(mixer, source, 3)
    data = signal(0, 480)
    try:
        for _ in range(3):
            api.bh_mix_write(mixer, source, data.ctypes.data, 480)
        render(api, mixer)
        for _ in range(15):
            api.bh_mix_write(mixer, source, data.ctypes.data, 480)
        render(api, mixer)
        assert api.bh_mix_value(mixer, source, 1) > 0
        assert api.bh_mix_value(mixer, source, 18) > 0
        assert api.bh_mix_value(mixer, source, 0) <= 960
    finally:
        api.bh_mix_free(mixer)


def test_future_snapshot_timestamp_saturates_instead_of_underflowing_to_an_old_age():
    with NativeClient(spawn=True) as client:
        for now, stamp, expected in [(100, 99, 1), (100, 100, 0), (100, 101, 0),
                                      (2**63, 2**63+1, 0)]:
            assert client.request('test_elapsed', now=now, stamp=stamp) == expected


@pytest.mark.parametrize('direction', ['input', 'output'])
def test_unchanged_healthy_route_is_not_reopened_and_explicit_rescan_uses_native_owner(direction):
    with NativeClient(spawn=True) as client:
        client.wait_audio(client.request('test_audio_switch', direction=direction, endpoint_id='good'))
        result = client.wait_audio(client.request('test_audio_switch', direction=direction, endpoint_id='good'))
        assert result['unchanged']
        assert client.retry_audio() == []
        assert client.request('snapshot')['microphone_active' if direction == 'input' else 'output_active']


def test_rescan_does_not_replace_a_newer_stop_request_with_an_old_start_intent():
    with NativeClient(spawn=True) as client:
        client.wait_audio(client.request('test_audio_switch', direction='input', endpoint_id='good'))
        stop = client.request('test_audio_switch', direction='input', start=False, delay_ms=100)
        assert client.request('audio_retry') == []
        client.wait_audio(stop)
        assert not client.request('snapshot')['microphone_active']


def test_recovery_notices_are_coalesced_and_wait_for_stable_health_before_reporting_restored():
    observer = AudioHealth()
    value = dict(state='retrying', category='audio_endpoint', native_code=-204, automatic=True, attempts=1)
    def sample():
        return observer.events(dict(continuity=dict(devices=dict(recovery=dict(output=value)))))
    assert sample() == []
    value['attempts'] = 2
    assert '稍后重试' in sample()[0]['message']
    for attempt in range(3, 100):
        value['attempts'] = attempt
        assert sample() == []
    value.update(state='needs_attention', automatic=False)
    assert '请检查后重试' in sample()[0]['message']
    value['state'] = 'recovering'
    assert sample() == []
    value['state'] = 'healthy'
    assert sample() == [dict(direction='output', state='restored', message='播放设备已恢复')]
    assert sample() == []


@pytest.fixture(scope='module')
def float_environment(tmp_path_factory):
    folder = tmp_path_factory.mktemp('float-environment')
    source = folder/'environment.c'
    source.write_text('#include <xmmintrin.h>\n'
                      '__declspec(dllexport) unsigned get_mode(void) { return _mm_getcsr(); }\n'
                      '__declspec(dllexport) void set_mode(unsigned mode) { _mm_setcsr(mode); }\n', 'utf-8')
    compiler = find_compiler()
    if not Path(compiler).is_file():
        pytest.skip('Native float environment probe needs MinGW')
    library = folder/'environment.dll'
    subprocess.run([compiler, '-shared', '-O2', '-Wall', '-Wextra', '-Werror', '-static-libgcc',
                    '-Wl,--no-insert-timestamp', str(source), '-o', str(library)], check=True)
    api = C.CDLL(str(library))
    api.get_mode.restype = C.c_uint
    api.set_mode.argtypes = [C.c_uint]
    return api


def test_dsp_environment_is_restored_even_on_validation_failure(float_environment):
    api = timeline_api()
    api.bh_capture_read_f32.argtypes = [C.c_void_p, C.c_void_p, C.c_uint]
    api.bh_render_process.argtypes = [C.c_void_p, C.c_void_p, C.c_uint]
    original = float_environment.get_mode()
    requested = (original & ~0x8040) | 0x20
    try:
        for call in (lambda: api.bh_mix_render(None, None, 480, 1),
                     lambda: api.bh_capture_read_f32(None, None, 480),
                     lambda: api.bh_render_process(None, None, 480)):
            float_environment.set_mode(requested)
            assert call() == -1
            assert float_environment.get_mode() == requested
    finally:
        float_environment.set_mode(original)


@pytest.mark.parametrize('amplitude,expect_silence', [(1e-35, False), (1e-39, True)])
def test_normal_float_detail_is_preserved_while_inaudible_denormals_cannot_spike_callback_cost(amplitude, expect_silence):
    api = timeline_api()
    mixer = api.bh_mix_create()
    source = api.bh_mix_add(mixer, 0, 8640)
    api.bh_mix_continuity(mixer, source, 1, 0, 0)
    api.bh_mix_timeline(mixer, source, 1)
    data = np.full((1200, 2), amplitude, 'f4')
    try:
        api.bh_mix_write(mixer, source, data.ctypes.data, len(data))
        out = render(api, mixer, 1200)
        if expect_silence:
            assert not out.any()
        else:
            np.testing.assert_array_equal(out[72:], data[:-72])
    finally:
        api.bh_mix_free(mixer)


def eventually(client, predicate, timeout=4):
    until = time.monotonic()+timeout
    while time.monotonic() < until:
        value = client.request('snapshot')
        if predicate(value):
            return value
        time.sleep(.02)
    raise AssertionError('Native recovery did not reach its expected state')


@pytest.mark.parametrize('direction', ['input', 'output'])
def test_endpoint_disappearance_falls_back_once_and_restores_the_original_intent_on_return(direction):
    with NativeClient(spawn=True) as client:
        client.wait_audio(client.request('test_audio_switch', direction=direction, endpoint_id='usb'))
        client.request('test_inventory', endpoints=[dict(id='fallback', default=True, direction=direction)])
        eventually(client, lambda state: client.request('test_routes')[direction] == '')
        assert client.request('test_routes')['wanted_'+direction] == 'usb'
        count = client.request('snapshot')['device_reopens']
        time.sleep(1.2)
        assert client.request('snapshot')['device_reopens'] == count
        client.request('test_inventory', endpoints=[dict(id='fallback', default=True, direction=direction), dict(id='usb', direction=direction)])
        eventually(client, lambda state: client.request('test_routes')[direction] == 'usb')
        assert client.request('test_routes')['wanted_'+direction] == 'usb'
        assert not client.request('snapshot')['usb']['active']


def test_actual_control_owner_applies_backoff_then_recovers_without_a_new_user_route():
    with NativeClient(spawn=True) as client:
        client.wait_audio(client.request('test_audio_switch', direction='output', endpoint_id='usb'))
        client.request('test_inventory', endpoints=[dict(id='fallback', default=True)], output_error=-19)
        state = eventually(client, lambda s: s['devices']['recovery']['output']['failures'] >= 2)
        assert state['devices']['recovery']['output']['automatic']
        assert state['device_reopens'] <= 3
        client.request('test_inventory', endpoints=[dict(id='fallback', default=True)])
        state = eventually(client, lambda s: s['devices']['recovery']['output']['recoveries'] >= 1)
        assert state['devices']['recovery']['output']['recoveries'] == 1
        assert client.request('test_routes')['wanted_output'] == 'usb'


def test_permission_failure_stops_automatic_retry_until_explicit_rescan():
    with NativeClient(spawn=True) as client:
        client.wait_audio(client.request('test_audio_switch', direction='output', endpoint_id='usb'))
        client.request('test_inventory', endpoints=[dict(id='fallback', default=True)], output_error=-6)
        state = eventually(client, lambda s: s['devices']['recovery']['output']['state'] == 'needs_attention')
        failures = state['devices']['recovery']['output']['failures']
        time.sleep(1.2)
        assert client.request('snapshot')['devices']['recovery']['output']['failures'] == failures
        client.request('test_inventory', endpoints=[dict(id='fallback', default=True)])
        assert client.retry_audio() == []
        assert client.request('test_routes')['output'] == ''
        assert client.request('test_routes')['wanted_output'] == 'usb'


def test_one_malformed_packet_does_not_rebuild_a_working_native_decoder():
    with NativeClient(spawn=True) as client:
        source(client)
        def send(data):
            acl = struct.pack('<HHHH', 1, len(data)+4, len(data), 64)+data
            assert client.request('test_hci_acl', packet=acl.hex())['handled']
        for i in range(25):
            send(packet(i))
        eventually(client, lambda s: s['sources']['test']['media']['decoded_frames'] > 2000)
        send(b'\x00')
        for i in range(25, 60):
            send(packet(i))
        state = eventually(client, lambda s: s['sources']['test']['media']['malformed_packets'] == 1)
        assert state['sources']['test']['media']['decoder_restarts'] == 0
        assert client.request('test_render', frames=480)['rms'] > 0


@pytest.mark.parametrize('skipped', [4, 5])
def test_cvsd_receive_expiry_keeps_pcm_word_alignment_across_odd_packet_boundaries(dll, skipped):  # noqa: F811 imported pytest fixture
    dll.bh_voice_skip_rx.argtypes = [C.c_void_p, C.c_uint]
    dll.bh_voice_reset_rx.argtypes = [C.c_void_p]
    actual, clean = Voice(dll, 1), Voice(dll, 1)
    raw = np.rint(12000*np.sin(np.arange(2400)*2*np.pi*397/8000)).astype('<i2').tobytes()
    try:
        actual.push(raw[:3])  # one complete PCM word and a pending low byte
        assert dll.bh_voice_skip_rx(actual.handle, skipped) == 0
        assert dll.bh_voice_reset_rx(actual.handle) == 0
        start = 3+skipped
        expected = start+(start & 1)
        outputs, references = [], []
        for at in range(0, 2400, 120):
            actual.push(raw[start+at:start+at+120])
            clean.push(raw[expected+at:expected+at+120])
            outputs.append(actual.read())
            references.append(clean.read())
        # A skipped high byte changes only when the first frame is completed.
        a, b = np.concatenate(outputs), np.concatenate(references)
        np.testing.assert_array_equal(a, b[:len(a)])
    finally:
        actual.close()
        clean.close()


@pytest.mark.parametrize('direction', ['media_commit', 'voice_commit'])
def test_packet_deadline_is_rechecked_after_slow_codec_work_before_pcm_is_published(direction):
    with NativeClient(spawn=True) as client:
        client.request('test_replay', seconds=3, codec='aac', voice_codec=3)
        time.sleep(.7)
        client.request('test_stall', direction=direction, milliseconds=300)
        report = wait_done(client)
        state = client.request('snapshot')
        if direction == 'media_commit':
            assert state['sources']['offline:media']['media']['expired_packets'] > 0
        else:
            assert state['voice']['expired_packets'] > 0 and state['voice']['rx_resets'] >= 1
        assert report['codec_errors'] == 0
        assert state['voice']['decode_errors'] == 0


def test_hangup_clears_all_old_voice_pcm_and_removed_source_cannot_write_into_a_reused_slot(dll):  # noqa: F811 imported pytest fixture
    sender = Voice(dll, 3)
    try:
        with NativeClient(spawn=True) as client:
            client.request('mix_config', call_buffer_ms=0)
            original = client.request('mix_add', key='call:a')
            epoch = client.request('voice_start', key='a', handle=2, codec=3)['epoch']
            for i in range(12):
                client.request('test_voice_rx', handle=2, packet=sender.encode(tone(i)).hex())
            eventually(client, lambda s: s['voice']['decoded_frames'] >= 3000)
            assert client.request('test_render', frames=480)['rms'] > .01
            client.request('voice_stop', key='a', epoch=epoch)
            assert client.request('test_render', frames=480)['rms'] == 0
            client.request('mix_remove', key='call:a', lease=original['lease'])
            replacement = source(client, key='replacement')
            assert replacement['index'] == original['index']
            for i in range(40):
                data = packet(i)
                acl = struct.pack('<HHHH', 1, len(data)+4, len(data), 64)+data
                client.request('test_hci_acl', packet=acl.hex())
            eventually(client, lambda s: s['sources']['replacement']['media']['decoded_frames'] > 3000)
            client.request('voice_stop')  # redundant cleanup must not clear the new media slot
            assert client.request('test_render', frames=480)['rms'] > 0
    finally:
        sender.close()


def test_removing_an_active_voice_source_stops_its_worker_before_the_slot_can_be_reused():
    with NativeClient(spawn=True) as client:
        original = client.request('mix_add', key='call:a')
        client.request('voice_start', key='a', handle=2, codec=3)
        client.request('mix_remove', key='call:a', lease=original['lease'])
        assert not client.request('snapshot')['voice']['active']
        replacement = source(client, key='replacement')
        assert replacement['index'] == original['index']
        assert not client.request('test_voice_rx', handle=2, packet='0000')
        assert client.request('snapshot')['sources']['replacement']['gap_owner'] == 1


def test_expired_session_views_and_cleanup_cannot_touch_a_replacement_or_hide_real_io_failures():
    with NativeClient(spawn=True) as client:
        mixer = RemoteMixer(client)
        events = []
        old = RemoteMediaSession('device', mixer, lambda *args, **kwargs: events.append(args))
        lease = old.input.lease
        client.request('mix_remove', key='device', lease=lease)
        newer = client.request('mix_add', key='device')
        with pytest.raises(SourceExpired):
            client.request('mix_clear', key='device', lease=lease)
        assert old.snapshot() == {} and old.last_packet == 0
        def broken(*args, **kwargs):
            raise OSError('native channel lost')
        old.client = SimpleNamespace(request=broken)
        with pytest.raises(OSError, match='native channel lost'):
            old.snapshot()
        old.client = client
        old.configure(Format('sbc', 48000, 2))
        assert not events
        old.pause()
        old.resume()
        mixer.set_gain('device', 2)
        old.close()
        assert 'device' in client.request('snapshot')['sources']
        assert newer['lease'] != lease


async def test_inventory_refresh_cannot_bypass_native_retry_latches_and_restores_preferred_routes(monkeypatch, tmp_path):
    forced, output_changes, input_changes = [], [], []
    client = SimpleNamespace(retry_audio=lambda: forced.append(True) or [])
    receiver = Receiver(Settings(output_endpoint_id='preferred-output', input_endpoint_id='preferred-input'),
                        tmp_path, lambda *args, **kwargs: None, mixer=SimpleNamespace(remote=True, client=client))
    mapped = dict(output=None, input=None)
    monkeypatch.setattr('bluehead.audio_runtime.refresh_devices', lambda *args: (dict(mapped), []))
    output = receiver.output = SimpleNamespace(endpoint_id=None, device=None)
    def switch(index):
        output_changes.append(index)
        output.device, output.endpoint_id = index, 'preferred-output'
    output.switch_device = switch
    calls = receiver.calls = SimpleNamespace(input_lock=asyncio.Lock(), input_device=None)
    async def set_input(index):
        assert not calls.input_lock.locked()
        input_changes.append(index)
        calls.input_device = index
    calls.set_input_device = set_input
    await receiver.refresh_audio()
    assert forced == []
    assert receiver.settings.output_endpoint_id == 'preferred-output'
    assert receiver.settings.input_endpoint_id == 'preferred-input'
    await receiver.refresh_audio(force=True)
    assert forced == [True]
    mapped.update(output=4, input=5)
    await receiver.refresh_audio()
    await receiver.refresh_audio()
    assert output_changes == [4] and input_changes == [5]
    assert forced == [True]


def test_startup_keeps_missing_preferred_endpoint_identity_for_later_return(monkeypatch, tmp_path):
    def missing(*args):
        raise ValueError('endpoint missing')
    monkeypatch.setattr('bluehead.endpoints.find_endpoint', missing)
    engine = Engine(tmp_path, StringIO())
    settings = Settings(mode='advanced', output_endpoint_id='preferred-output', input_endpoint_id='preferred-input')
    engine.remap_saved_audio(settings)
    assert settings.output_device is settings.input_device is None
    assert settings.output_endpoint_id == 'preferred-output'
    assert settings.input_endpoint_id == 'preferred-input'


async def test_native_channel_failure_is_distinct_from_device_loss_and_cleanup_keeps_the_original_cause():
    class Client:
        def request(self, op, **values):
            if op == 'usb_open':
                return True
            raise OSError('native channel lost')
    transport = NativeTransport(Client(), 'usb:8087:0033#0')
    with pytest.raises(RuntimeError, match='original operation failed'):
        async with transport:
            await asyncio.wait_for(transport.source.terminated, 1)
            assert isinstance(transport.source.failure, OSError)
            raise RuntimeError('original operation failed')


@pytest.mark.parametrize('replace_epoch', [False, True])
def test_voice_telemetry_cannot_read_deleted_codec_state_or_attribute_another_sessions_audio(replace_epoch):
    manager = NativeCallManager.__new__(NativeCallManager)
    link = object()
    manager.links = {'a': link}
    manager.voice_codecs = {'a': SimpleNamespace(codec=3)}
    manager.native_epochs = {'a': 1}
    manager.statistics = {'a': dict(codec='lc3_swb', rate=32000)}
    manager.native_meters = {}
    def snapshot(*args):
        if not replace_epoch:
            manager.links.clear()
            manager.voice_codecs.clear()
            manager.statistics.clear()
        return dict(voice=dict(epoch=2 if replace_epoch else 1), uplink={})
    manager.client = SimpleNamespace(request=snapshot)
    assert manager.snapshot('a') == {}
    assert manager.native_meters == {}


async def test_retirement_attempts_all_resources_and_preserves_original_failure():
    attempted = []
    retirement = Retirement()
    first = OSError('first close failure')
    def broken():
        attempted.append('first')
        raise first
    async def later():
        attempted.append('second')
        raise RuntimeError('second close failure')
    retirement.call(broken)
    await retirement.async_call(later)
    retirement.call(attempted.append, 'last')
    assert attempted == ['first', 'second', 'last']
    with pytest.raises(OSError) as caught:
        retirement.finish()
    assert caught.value is first
    retirement.finish(RuntimeError('original operation failed'))


async def test_native_call_manager_close_retires_active_voice_epoch_and_remains_idempotent():
    from .test_protocols import Network
    with NativeClient(spawn=True) as client:
        mixer = RemoteMixer(client)
        manager = NativeCallManager(Network(1).devices[0], mixer, lambda *args, **kwargs: None)
        mixer.add('call:peer')
        epoch = client.request('voice_start', key='peer', handle=2, codec=3)['epoch']
        manager.links['peer'] = object()
        manager.native_epochs['peer'] = epoch
        manager.native_meters['peer'] = (0, 0, 0)
        manager.voice_codecs['peer'] = SimpleNamespace(close=lambda: client.request('voice_stop', key='peer', epoch=epoch))
        manager.router.select('peer')
        manager.close()
        await manager.wait_closed()
        manager.close()
        assert not manager.native_epochs and not manager.native_meters
        assert not client.request('snapshot')['voice']['active']


@pytest.mark.parametrize('reason,failed', [('device_removed', False), ('control_backpressure', True),
                                         ('usb_submit_failed', True), ('usb_transfer_failed', True)])
async def test_transport_terminal_reason_distinguishes_removal_from_software_or_io_failure(reason, failed):
    class Client:
        def request(self, op, **values):
            return dict(packets=[], active=False, reason=reason) if op == 'usb_poll' else True
    async with NativeTransport(Client(), 'usb:8087:0033#0') as transport:
        await asyncio.wait_for(transport.source.terminated, 1)
        assert bool(transport.source.failure) == failed
        assert transport.source.failure_category == (reason if failed else '')

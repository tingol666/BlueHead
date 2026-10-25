import asyncio
from dataclasses import asdict
from io import StringIO
import json

import numpy as np
import pytest
from bumble import avdtp

from bluehead.audio import Mixer
from bluehead.backend import Receiver, Settings
from bluehead.continuity import ContinuityGuard
from bluehead.engine import Engine, validate_settings
from bluehead.protocol import capabilities, negotiated, PacketError
from .test_protocols import Network, open_media


def test_auto_profile_prioritizes_usable_high_quality_and_keeps_lower_profiles_selectable(tmp_path):
    r = Receiver(Settings(), tmp_path, lambda *a, **k: None)
    assert r.media_codecs() == ["aptx_hd", "aptx", "aac", "sbc"]
    r.settings.ldac_enabled = True
    assert r.media_codecs()[0] == "ldac"
    for codec in ("aptx_hd", "aptx", "aac_192", "ldac"):
        r.settings.codec, r.settings.ldac_enabled = codec, True
        assert r.media_codecs() == [codec, "sbc"]


def test_low_bitrate_aac_cap_is_negotiated_and_enforced():
    offered = capabilities("aac_192")
    assert int.from_bytes(bytes(offered.media_codec_information)[3:], "big") & 0x7fffff == 192000
    fmt = negotiated("aac_192", [capabilities("aac_192", selected=True)])
    assert fmt.codec == "aac" and fmt.rate == 48000
    with pytest.raises(PacketError, match="maximum"):
        negotiated("aac_192", [capabilities("aac", selected=True)])


@pytest.mark.parametrize("field,value", [("media_mode", "both-ish"), ("codec", "aac_999")])
def test_profile_rejects_invalid_values(field, value):
    data = asdict(Settings(transport="usb:8087:0033#0"))
    data[field] = value
    with pytest.raises(ValueError):
        validate_settings(data)


def test_output_mode_saved_and_legacy_settings_default_to_mix(tmp_path):
    (tmp_path / "settings.json").write_text('{"codec":"aptx_hd"}', "utf-8")
    output = StringIO()
    engine = Engine(tmp_path, output)
    assert engine.settings.media_mode == "mix" and engine.settings.codec == "aptx_hd"
    engine.command({"protocol": 1, "command": "media_mode", "mode": "single"})
    assert json.loads((tmp_path / "settings.json").read_text("utf-8"))["media_mode"] == "single"
    assert json.loads(output.getvalue())["success"]


def test_guard_ignores_isolated_loss_then_quarantines_and_recovers_without_flapping():
    now = [0.0]
    guard = ContinuityGuard(clock=lambda: now[0])
    def window(supply=1, lost=0, errors=0):
        # 20-ms packets, exact fake clock; no real audio or sleeping.
        for _ in range(26):
            guard.observe(int(960 * supply), 1, lost, errors, True)
            now[0] += .02
    window(lost=1)
    assert not guard.protected
    window(.2, lost=1, errors=1)
    window(.2, lost=1, errors=1)
    window(.2, lost=1, errors=1)
    assert guard.protected
    window()
    assert guard.protected  # one clean window is insufficient
    for _ in range(4):
        window()
    assert not guard.protected
    window(.1, errors=1)
    window(.1, errors=1)
    window(.1, errors=1)
    assert guard.protected
    guard.observe(0, 0, 0, 0, False)
    assert not guard.protected


@pytest.mark.parametrize("native", [True, False])
def test_gate_is_independent_of_user_gains_and_voice_and_consumes_stale_pcm(native):
    mixer = Mixer(buffer_ms=0, native=native)
    for key in ("good", "damaged", "call:voice"):
        mixer.add(key)
    mixer.set_gain("damaged", 1.8)
    pcm = np.full((960, 2), .1, np.float32)
    mixer.write("good", pcm)
    mixer.write("damaged", pcm)
    mixer.write("call:voice", pcm)
    mixer.render(1)  # Start the source before testing the fade envelope.
    mixer.set_enabled("damaged", False)
    out = mixer.render(959)
    # Ten-ms fade completes. Voice and the other source are unaffected.
    np.testing.assert_allclose(out[481:], .2, atol=1e-6)
    assert mixer.inputs["damaged"].gain == 1.8 and mixer.inputs["damaged"].frames == 0
    assert np.max(np.abs(np.diff(out[:, 0]))) < .001
    mixer.set_enabled("damaged", True)
    mixer.write("damaged", pcm)
    out = mixer.render(960)
    np.testing.assert_allclose(out[481:], .18, atol=1e-6)


@pytest.mark.asyncio
async def test_single_source_policy_suspends_radio_stream_then_switches_and_restores_mix(tmp_path):
    network = Network(3)
    mixer = Mixer(buffer_ms=0)
    r = Receiver(Settings(calls=False, media_mode="single"), tmp_path, lambda *a, **k: None, mixer)
    r.attach(network.devices[0])
    await network.start()
    try:
        async with asyncio.timeout(12):
            ca, a = await open_media(network.devices[1], network.devices[0], "aac")
            cb, b = await open_media(network.devices[2], network.devices[0], "sbc")
            ka, kb = str(ca.self_address), str(cb.self_address)
            while b.state != avdtp.State.OPEN:
                await asyncio.sleep(.01)
            assert a.state == avdtp.State.STREAMING
            assert r.sessions[ka].allowed and not r.sessions[kb].allowed
            r.set_device_gain(ka, "down", 1.75)
            await r.select_media(kb)
            assert a.state == avdtp.State.OPEN and b.state == avdtp.State.STREAMING
            assert not r.sessions[ka].allowed and r.sessions[kb].allowed
            assert mixer.inputs[ka].gain == 1.75
            await r.set_media_mode("mix")
            assert a.state == b.state == avdtp.State.STREAMING
            assert all(s.allowed for s in r.sessions.values())
            await r.set_media_mode("single")
            await cb.disconnect()
            while a.state != avdtp.State.STREAMING:
                await asyncio.sleep(.01)
            assert r.media_owner == ka and r.sessions[ka].allowed
            await ca.disconnect()
    finally:
        for s in list(r.sessions.values()):
            s.close()
        await network.close()


def test_codec_restart_joins_old_receiver_cleanup_before_start(tmp_path, monkeypatch):
    import threading
    engine = Engine(tmp_path, StringIO())
    release = threading.Event()
    previous = threading.Thread(target=lambda: release.wait(1))
    previous.start()
    engine.runner.thread = previous
    engine.runner.stop_requested.set()
    requested = []
    monkeypatch.setattr(engine.runner, "start", lambda settings, directory: requested.append(settings.codec))
    monkeypatch.setattr(engine, "remap_saved_audio", lambda settings: None)
    timer = threading.Timer(.03, release.set)
    timer.start()
    try:
        engine.command({"protocol": 1, "command": "start", "settings": asdict(Settings(transport="usb:8087:0033#0", codec="aac_192"))})
        assert requested == ["aac_192"] and not previous.is_alive()
    finally:
        release.set()
        previous.join(1)
        timer.cancel()


def test_closed_media_callbacks_cannot_clear_replacement_source():
    from bluehead.backend import MediaSession
    mixer = Mixer(buffer_ms=0)
    old = MediaSession("reused-device", mixer, lambda *a, **k: None)
    old.close()
    mixer.add("reused-device")
    pcm = np.full((960, 2), .2, np.float32)
    mixer.write("reused-device", pcm)
    old.pause()
    old.resume()
    np.testing.assert_allclose(mixer.render(480), .2, atol=1e-6)


@pytest.mark.asyncio
async def test_single_media_auto_handoff_on_remote_pause_and_no_packet_gap(tmp_path):
    import time
    network = Network(3)
    r = Receiver(Settings(calls=False, media_mode="single"), tmp_path, lambda *a, **k: None, Mixer(buffer_ms=0))
    r.media_idle_timeout = .25
    r.media_pause_delay = .03
    r.attach(network.devices[0])
    await network.start()
    async def wait_for(predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(.01)
    try:
        ca, a = await open_media(network.devices[1], network.devices[0], "aac")
        cb, b = await open_media(network.devices[2], network.devices[0], "sbc")
        ka, kb = str(ca.self_address), str(cb.self_address)
        await wait_for(lambda: b.state == avdtp.State.OPEN)
        r.sessions[ka].last_packet = time.monotonic()
        await a.stop()
        await wait_for(lambda: r.media_owner == kb and b.state == avdtp.State.STREAMING)
        assert r.sessions[kb].allowed and not r.sessions[ka].allowed
        # Exercise actual decoded PCM after handoff, not only signalling and labels.
        from .vectors import signal, encode, wire_packets
        for packet in wire_packets("sbc", encode("sbc", signal(48000, .12))):
            b.rtp_channel.write(packet)
            await asyncio.sleep(.003)
        await wait_for(lambda: r.mixer.inputs[kb].frames > 2000)
        assert np.std(r.mixer.render(2000)) > .005 and r.sessions[kb].errors == 0
        # A was user-paused, so B going idle must not repeatedly restart A.
        await asyncio.sleep(.4)
        assert r.media_owner == kb and a.state == avdtp.State.OPEN
        # A new user play request waits for B, then takes over on packet silence.
        await a.start()
        await wait_for(lambda: r.media_owner == ka and a.state == avdtp.State.STREAMING)
        assert r.sessions[ka].allowed and not r.sessions[kb].allowed
        await ca.disconnect()
        await cb.disconnect()
    finally:
        r.stop_event.set()
        if r.media_watch_task:
            r.media_watch_task.cancel()
            try:
                await r.media_watch_task
            except asyncio.CancelledError:
                pass
        for session in list(r.sessions.values()):
            session.close()
        await network.close()


@pytest.mark.asyncio
async def test_handoff_uses_packet_activity_and_keeps_voice_priority_and_resume_grace(tmp_path, monkeypatch):
    from types import SimpleNamespace
    r = Receiver(Settings(calls=False, media_mode="single"), tmp_path, lambda *a, **k: None)
    r.media_owner = "a"
    r.connections = {"a": object(), "b": object()}
    r.sessions = {"a": SimpleNamespace(closed=False, paused=False, started_at=1, last_packet=10, paused_at=0),
                  "b": SimpleNamespace(closed=False)}
    r.media_sinks["b"] = object()
    r.media_waiting["b"] = 9
    requested = []
    async def select(key):
        requested.append(key)
        r.media_owner = key
    monkeypatch.setattr(r, "_select_media", select)
    await r.advance_media(10.2)
    assert not requested  # Even all-zero PCM is active while packets arrive.
    r.mixer.call_active = True
    await r.advance_media(20)
    assert not requested
    r.mixer.call_active = False
    await r.advance_media(20.3)
    assert not requested  # Allow A2DP to resume after a voice link ends.
    await r.advance_media(20.8)
    assert requested == ["b"]


@pytest.mark.asyncio
async def test_idle_single_source_without_waiter_resumes_without_another_start(tmp_path):
    from types import SimpleNamespace
    r = Receiver(Settings(calls=False, media_mode="single"), tmp_path, lambda *a, **k: None)
    r.media_owner = "a"
    r.connections = {"a": object()}
    r.sessions = {"a": SimpleNamespace(closed=False, paused=False, started_at=1, last_packet=1, paused_at=0)}
    await r.advance_media(30)
    assert r.media_owner == "a"  # Keep its gate open for resumed RTP packets.


@pytest.mark.parametrize("terminal", [False, True])
def test_remote_pause_cancels_auto_resume_without_touching_replacement(tmp_path, terminal):
    from bluehead.backend import MediaSession
    r = Receiver(Settings(calls=False, media_mode="single"), tmp_path, lambda *a, **k: None, Mixer(buffer_ms=0))
    old = MediaSession("b", r.mixer, r.emit)
    r.sessions["b"] = old
    r.media_waiting["b"] = 1
    r.policy_suspended.add("b")
    sink = object()
    r.media_sinks["b"] = sink
    try:
        r.media_paused("b", old, terminal)
        assert old.paused and "b" not in r.media_waiting and "b" not in r.policy_suspended
        assert ("b" not in r.media_sinks) == terminal
        r.media_waiting["b"] = 2
        replacement = object()
        r.sessions["b"] = replacement
        r.media_sinks["b"] = sink
        r.media_paused("b", old, True)
        assert r.media_waiting["b"] == 2 and r.media_sinks["b"] is sink
    finally:
        old.close()

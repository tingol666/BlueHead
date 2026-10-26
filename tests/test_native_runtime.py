"""Native host integration, using synthetic HCI only; never opens hardware."""
import base64
import ctypes
import json
from pathlib import Path
import struct
import subprocess
import sys
import time

import pytest

from bluehead import __version__
from bluehead.native_runtime import NativeClient

ROOT = Path(__file__).resolve().parents[1]


def packet(sequence=0):
    fixture = json.loads((ROOT / "bluehead/assets/selfcheck_vectors.json").read_text("utf-8"))
    vector = next(v for v in fixture["vectors"] if v["codec"] == "sbc")
    data = bytearray(base64.b64decode(vector["packets"][sequence % len(vector["packets"])]))
    struct.pack_into(">HI", data, 2, sequence & 65535, (sequence * 128) & 0xffffffff)
    return bytes(data)


def source(client, key="test"):
    session = client.request("mix_add", key=key)
    client.request("media_config", key=key, lease=session["lease"], codec=1, rate=48000, channels=2)
    client.request("media_route", key=key, handle=1, cid=64)
    client.request("media_pause", key=key, paused=False)
    return session


def wait_done(client, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = client.request("test_replay_status")
        if result.get("done"):
            return result
        time.sleep(.02)
    raise AssertionError("Native replay did not finish")


def test_native_handshake_and_hardware_isolation():
    with NativeClient(spawn=True) as client:
        assert client.request("hello")["version"] == __version__
        for op in ("usb_open", "audio_switch", "system_audio"):
            with pytest.raises(OSError, match="offline-test"):
                client.request(op)
        assert not client.request("snapshot")["usb"]["active"]


def test_native_production_rejects_injection():
    with NativeClient(spawn=True, offline=False) as client:
        with pytest.raises(OSError, match="offline-test"):
            client.request("test_replay")
        with pytest.raises(OSError, match="offline-test"):
            client.request("test_hci_acl", packet="")


def test_native_expired_source_lease_cannot_touch_replacement():
    with NativeClient(spawn=True) as client:
        first = source(client)
        client.request("mix_remove", key="test", lease=first["lease"])
        second = source(client)
        assert first["index"] == second["index"]
        assert first["lease"] != second["lease"]
        for op in ("media_config", "mix_remove"):
            with pytest.raises(OSError, match="expired"):
                client.request(op, key="test", lease=first["lease"])
        assert "test" in client.request("snapshot")["sources"]


def test_native_hci_fragment_reassembly_reaches_codec():
    with NativeClient(spawn=True) as client:
        source(client)
        for i in range(40):
            data = packet(i)
            first = struct.pack("<HHHH", 1, 24, len(data), 64) + data[:20]
            tail = struct.pack("<HH", 0x1001, len(data) - 20) + data[20:]
            assert client.request("test_hci_acl", packet=first.hex())["handled"]
            assert client.request("test_hci_acl", packet=tail.hex())["handled"]
        end = time.monotonic() + 2
        while time.monotonic() < end:
            stats = client.request("media_snapshot", key="test")
            if stats["decoded_frames"] > 3000:
                break
            time.sleep(.01)
        assert stats["decode_errors"] == 0
        assert stats["decoded_frames"] > 3000
        assert client.request("test_render", frames=480)["rms"] > 0


@pytest.mark.parametrize("codec,voice", [("sbc", 0), ("aac", 3)])
def test_native_playout_continues_while_python_holds_gil(codec, voice):
    with NativeClient(spawn=True) as client:
        client.request("test_replay", seconds=6, codec=codec, voice_codec=voice)
        time.sleep(1)
        # PyDLL keeps the GIL throughout this Windows wait.
        ctypes.PyDLL("kernel32").Sleep(4000)
        report = wait_done(client)
        assert report["codec_errors"] == report["compressed_queue_drops"] == 0
        assert report["maximum_zero_run_ms"] < 1
        assert report["underruns"] == 0
        if voice:
            assert report["voice_rx_packets"] > 700
            assert report["voice_tx_packets"] > 700


def test_native_short_loss_avoids_full_rebuffer():
    with NativeClient(spawn=True) as legacy, NativeClient(spawn=True) as current:
        args = dict(seconds=7, drop_every=31, jitter_ms=3)
        legacy.request("test_replay", legacy_continuity=True, **args)
        current.request("test_replay", **args)
        old, new = wait_done(legacy), wait_done(current)
        assert old["maximum_zero_run_ms"] >= 30
        assert new["maximum_zero_run_ms"] < 5
        assert new["prefill_frames"] < old["prefill_frames"]
        assert new["codec_errors"] == new["compressed_queue_drops"] == 0


def test_native_cancelled_prepare_cannot_commit_late():
    with NativeClient(spawn=True) as client:
        ticket = client.request("test_audio_switch", direction="output", delay_ms=300)
        with pytest.raises(TimeoutError):
            client.wait_audio(ticket, timeout=.03)
        time.sleep(.4)
        assert not client.request("snapshot")["output_active"]
        ticket = client.request("test_audio_switch", direction="output")
        assert client.wait_audio(ticket)["success"]
        assert client.request("snapshot")["output_active"]


def test_native_hangup_invalidates_pending_microphone_prepare():
    with NativeClient(spawn=True) as client:
        client.request("mix_add", key="call:a")
        epoch = client.request("voice_start", key="a", handle=2, codec=2)["epoch"]
        ticket = client.request("test_audio_switch", direction="input", delay_ms=200, voice_epoch=epoch)
        client.request("voice_stop", key="a", epoch=epoch)
        with pytest.raises(OSError):
            client.wait_audio(ticket)
        time.sleep(.3)
        assert not client.request("snapshot")["microphone_active"]


def test_native_reused_hci_route_rejects_old_partial_packet():
    with NativeClient(spawn=True) as client:
        first = source(client)
        p = packet(0)
        head = struct.pack("<HHHH", 1, 24, len(p), 64) + p[:20]
        tail = struct.pack("<HH", 0x1001, len(p) - 20) + p[20:]
        client.request("test_hci_acl", packet=head.hex())
        client.request("mix_remove", key="test", lease=first["lease"])
        source(client)
        client.request("test_hci_acl", packet=tail.hex())
        assert client.request("media_snapshot", key="test")["packets"] == 0


def test_native_voice_timer_concealment_stops_after_60ms():
    with NativeClient(spawn=True) as client:
        client.request("test_replay", seconds=.5, voice_codec=2)
        wait_done(client)
        time.sleep(.2)
        first = client.request("snapshot")["voice"]["plc_frames"]
        time.sleep(.3)
        assert client.request("snapshot")["voice"]["plc_frames"] == first
        assert first <= 8


def test_native_sco_completions_do_not_fill_python_control_queue_or_change_acl_credits():
    with NativeClient(spawn=True) as client:
        result = client.request("test_completions", packet="1309020100060002000a00", sco_handles=[2])
        assert result["packet"] == "13050101000600"
        assert result["sco_completed"] == 10
        result = client.request("test_completions", packet="13050102000a00", sco_handles=[2])
        assert result["packet"] == "" and result["sco_completed"] == 10
        for malformed in ("13", "130001", "1305010200", "13050102000a0000"):
            result = client.request("test_completions", packet=malformed, sco_handles=[2])
            assert result["handled"] and result["packet"] == ""


def test_native_capture_ready_event_drains_pcm_without_python_bridge():
    with NativeClient(spawn=True) as client:
        client.request("mix_add", key="call:a")
        epoch = client.request("voice_start", key="a", handle=2, codec=2)["epoch"]
        ticket = client.request("test_audio_switch", direction="input", voice_epoch=epoch)
        client.wait_audio(ticket)
        client.request("test_capture", frames=2400)
        time.sleep(.05)
        stats = client.request("snapshot")
        assert stats["microphone_frames"] == 2400
        assert stats["bridge_errors"] == 0


def test_native_pause_and_queued_pcm_commit_are_linearizable():
    with NativeClient(spawn=True) as client:
        client.request("test_replay", seconds=3)
        time.sleep(.3)
        for _ in range(50):
            client.request("media_pause", key="offline:media", paused=True)
            time.sleep(.002)
            assert client.request("snapshot")["sources"]["offline:media"]["queued_frames"] == 0
            client.request("media_pause", key="offline:media", paused=False)
            time.sleep(.003)
        client.request("test_replay_stop")


def test_native_gains_reject_stale_sidecar_echo_and_survive_stop_restart():
    with NativeClient(spawn=True) as client:
        client.request("master_gain", bus="microphone", gain=1.7, revision=2)
        client.request("master_gain", bus="system", gain=.6, revision=3)
        client.request("master_gain", bus="playback", gain=.4, revision=4)
        client.request("mute", muted=True, revision=5)
        client.request("router_gain", device=1, microphone=1, system=1, muted=False)
        client.request("master_gain", bus="playback", gain=1)
        client.request("mute", muted=False)
        gains = client.request("snapshot")["gains"]
        assert gains["microphone"] == pytest.approx(1.7)
        assert gains["system"] == pytest.approx(.6)
        assert gains["playback"] == pytest.approx(.4)
        assert gains["microphone_muted"]
        client.request("mute", muted=False, revision=6)
        client.request("quiesce")
        assert client.request("snapshot")["gains"]["effective_playback"] == 0
        client.request("test_replay", seconds=.25)
        wait_done(client)
        gains = client.request("snapshot")["gains"]
        assert gains["effective_playback"] == pytest.approx(.4)
        assert not gains["microphone_muted"]


def test_native_rejected_codec_prepare_keeps_existing_voice_and_media():
    with NativeClient(spawn=True) as client:
        source(client)
        with pytest.raises(OSError, match="unavailable"):
            client.request("media_config", key="test", codec=0, rate=48000, channels=2)
        for i in range(35):
            p = packet(i)
            acl = struct.pack("<HHHH", 1, len(p) + 4, len(p), 64) + p
            client.request("test_hci_acl", packet=acl.hex())
        time.sleep(.03)
        media = client.request("media_snapshot", key="test")
        assert media["codec"] == 1 and media["decoded_frames"] > 4000
        client.request("mix_add", key="call:a")
        client.request("voice_start", key="a", codec=2, handle=2)
        with pytest.raises(OSError, match="unavailable"):
            client.request("voice_start", key="a", codec=0, handle=2)
        assert client.request("snapshot")["voice"]["active"]


def test_native_disconnect_retires_voice_timer_without_spinning_until_python_cleanup():
    from tools.native_soak import resources
    with NativeClient(spawn=True) as client:
        client.request("test_replay", seconds=.4, voice_codec=2)
        wait_done(client)
        client.request("test_disconnect", handle=2)
        before = resources(client.process)["cpu_seconds"]
        time.sleep(.25)
        after = resources(client.process)["cpu_seconds"]
        assert after - before < .08
        assert not client.request("snapshot")["voice"]["active"]


def test_native_microphone_transmits_without_incoming_voice_for_recording_apps():
    with NativeClient(spawn=True) as client:
        client.request("mix_add", key="call:recording")
        epoch = client.request("voice_start", key="recording", handle=2, codec=2)["epoch"]
        ticket = client.request("test_audio_switch", direction="input", voice_epoch=epoch)
        client.wait_audio(ticket)
        client.request("test_capture", frames=2400)
        sent = 0
        deadline = time.monotonic() + 1
        while sent < 10 and time.monotonic() < deadline:
            response = client.request("test_voice_tx")
            if response["packet"]:
                assert response["handle"] == 2
                assert len(bytes.fromhex(response["packet"])) == 60
                sent += 1
            client.request("test_capture", frames=480)
            time.sleep(.008)
        assert sent == 10
        assert client.request("snapshot")["voice"]["rx_packets"] == 0


def test_native_host_relays_versioned_control_and_exits_cleanly(tmp_path):
    executable = ROOT / "bluehead/assets/BlueHead.Runtime.exe"
    commands = [{"protocol": 1, "command": "hello"}, {"protocol": 1, "command": "shutdown"}]
    result = subprocess.run([str(executable), "--engine", "--source-root", str(ROOT),
                             "--data-dir", str(tmp_path / "data")],
                            input="".join(json.dumps(c) + "\n" for c in commands),
                            capture_output=True, text=True, encoding="utf-8", timeout=12,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    assert all(message["protocol"] == 1 for message in messages)
    assert next(message for message in messages if message["event"] == "hello")["version"] == __version__


def test_entire_python_control_process_can_be_suspended_while_native_audio_runs():
    program = '''
import json, sys, time
from bluehead.native_runtime import NativeClient
with NativeClient(spawn=True) as client:
    client.request("test_replay", seconds=6, voice_codec=2)
    print("ready", flush=True)
    sys.stdin.readline()
    while True:
        result=client.request("test_replay_status")
        if result.get("done"): break
        time.sleep(.02)
    print(json.dumps(result),flush=True)
'''
    child = subprocess.Popen([sys.executable, "-X", "utf8", "-c", program], cwd=ROOT,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
    api = ctypes.WinDLL("kernel32", use_last_error=True)

    class ThreadEntry(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("usage", ctypes.c_ulong), ("id", ctypes.c_ulong),
                    ("pid", ctypes.c_ulong), ("base", ctypes.c_long), ("delta", ctypes.c_long),
                    ("flags", ctypes.c_ulong)]

    api.CreateToolhelp32Snapshot.argtypes, api.CreateToolhelp32Snapshot.restype = [ctypes.c_ulong] * 2, ctypes.c_void_p
    api.Thread32First.argtypes = api.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(ThreadEntry)]
    api.OpenThread.argtypes, api.OpenThread.restype = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong], ctypes.c_void_p
    for name in ("SuspendThread", "ResumeThread", "CloseHandle"):
        getattr(api, name).argtypes = [ctypes.c_void_p]
    suspended = []
    snapshot = None
    try:
        assert child.stdout.readline().strip() == "ready"
        time.sleep(1)
        snapshot = api.CreateToolhelp32Snapshot(4, 0)
        assert snapshot not in (None, ctypes.c_void_p(-1).value)
        entry = ThreadEntry()
        entry.size = ctypes.sizeof(entry)
        more = api.Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.pid == child.pid:
                handle = api.OpenThread(2, False, entry.id)
                assert handle
                if api.SuspendThread(handle) == -1:
                    api.CloseHandle(handle)
                    raise AssertionError("Could not suspend the isolated test control process")
                suspended.append(handle)
            more = api.Thread32Next(snapshot, ctypes.byref(entry))
        assert suspended
        time.sleep(3)
    finally:
        for handle in reversed(suspended):
            api.ResumeThread(handle)
            api.CloseHandle(handle)
        if snapshot:
            api.CloseHandle(snapshot)
    try:
        output, error = child.communicate("continue\n", timeout=10)
        assert child.returncode == 0, error
        report = json.loads(output)
        assert report["voice_rx_packets"] > 700 and report["voice_tx_packets"] > 700
        assert report["underruns"] == report["codec_errors"] == 0
        assert report["maximum_zero_run_ms"] < 1
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(5)


@pytest.mark.parametrize("stop_first", [False, True])
def test_native_fast_exit_persists_last_device_master_and_mute_intent(tmp_path, stop_first):
    # Exercise the actual host and Python settings file, including the race
    # between native stdin, its forwarding thread, and interpreter startup.
    for attempt in range(3):
        directory = tmp_path / str(attempt)
        commands = [
            {"command": "master_gain", "bus": "playback", "gain": .4},
            {"command": "master_gain", "bus": "microphone", "gain": 1.7},
            {"command": "master_gain", "bus": "system", "gain": .6},
            {"command": "mute", "muted": True},
            {"command": "gain", "key": "offline-device", "direction": "down", "gain": .55},
            {"command": "gain", "key": "offline-device", "direction": "up", "gain": 1.8},
            *([{"command": "stop"}] if stop_first else []),
            {"command": "shutdown"},
        ]
        result = subprocess.run(
            [str(ROOT / "bluehead/assets/BlueHead.Runtime.exe"), "--engine", "--source-root", str(ROOT),
             "--data-dir", str(directory)],
            input="".join(json.dumps({"protocol": 1, **command}) + "\n" for command in commands),
            capture_output=True, text=True, encoding="utf-8", timeout=12,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert result.returncode == 0, result.stderr
        saved = json.loads((directory / "settings.json").read_text("utf-8"))
        assert saved["playback_gain"] == .4
        assert saved["microphone_gain"] == 1.7
        assert saved["system_gain"] == .6
        assert saved["microphone_muted"] is True
        assert saved["device_volumes"]["offline-device"] == {"down": .55, "up": 1.8}


def test_native_diagnostics_survive_endpoint_close_and_contain_media_counters():
    with NativeClient(spawn=True) as client:
        source(client)
        ticket = client.request("test_audio_switch", direction="output")
        client.wait_audio(ticket)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            snapshot = client.request("snapshot")
            if snapshot["devices"].get("output"):
                break
            time.sleep(.02)
        assert snapshot["devices"]["output"]["sample_rate"] == 48000
        assert "max_callback_us" in snapshot["devices"]["output"]
        assert "max_callback_excess_us" in snapshot["devices"]["output"]
        assert snapshot['quality']['limiter_lookahead_ms'] == 1.5
        assert "lost_packets" in snapshot["sources"]["test"]["media"]
        assert "drift_ppm" in snapshot["sources"]["test"]
        ticket = client.request("test_audio_switch", direction="output", start=False)
        client.wait_audio(ticket)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            snapshot = client.request("snapshot")
            if "output" not in snapshot["devices"]:
                break
            time.sleep(.02)
        assert "output" not in snapshot["devices"]

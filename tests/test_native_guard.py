"""Keep the established multipoint damage guard on the native production path."""
import time

from bluehead.continuity import ContinuityGuard
from bluehead.native_runtime import NativeClient


def test_native_stream_guard_matches_existing_hysteresis_and_single_source_rule():
    now = 0.0
    reference = ContinuityGuard(clock=lambda: now)
    events, expected = [], []
    windows = [(0, 0, 0, True)]
    windows += [(1000, 5, 3, True)] * 6
    windows += [(25000, 0, 0, True)] * 5
    windows += [(1000, 5, 3, True)] * 5
    windows += [(0, 20, 10, False)] * 3
    for i, (frames, lost, errors, competing) in enumerate(windows):
        now = i * .51
        events.append(dict(time=now, frames=frames, packets=30, lost=lost, errors=errors, competing=competing))
        expected.append(reference.observe(frames, 30, lost, errors, competing))
    assert any(expected) and not expected[-1]
    with NativeClient(spawn=True) as client:
        assert client.request("test_stream_guard", events=events) == expected


def test_damaged_competing_stream_is_gated_and_sole_remaining_stream_is_released():
    with NativeClient(spawn=True) as client:
        client.request("test_replay", seconds=4, damaged_source=True)
        time.sleep(1.6)
        bad = client.request("media_snapshot", key="offline:damaged")
        good = client.request("media_snapshot", key="offline:media")
        assert bad["protected"] and not good["protected"]
        assert bad["lost_packets"] > 50 and bad["decode_errors"] == 0
        client.request("media_pause", key="offline:media", paused=True)
        time.sleep(.15)
        assert not client.request("media_snapshot", key="offline:damaged")["protected"]
        client.request("test_replay_stop")

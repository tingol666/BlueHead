from types import SimpleNamespace

import pytest

from bluehead.audio import Mixer, SwitchableStream
from bluehead.backend import Receiver, Settings


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_endpoint_resampling_keeps_pitch_and_bounded_clock_error(rate):
    import numpy as np
    from bluehead.audio import OutputRenderer

    class Tone:
        rate = 48000
        position = 0

        def render(self, frames):
            tone = .2 * np.sin(2 * np.pi * 1000 * np.arange(self.position, self.position + frames) / self.rate)
            self.position += frames
            return np.repeat(tone[:, None], 2, axis=1).astype(np.float32)

    mixer = Tone()
    renderer = OutputRenderer(mixer, rate)
    output = np.concatenate([renderer.render(rate // 100) for _ in range(100)])
    peak = np.argmax(abs(np.fft.rfft(output[:, 0])))
    assert peak == 1000 and .13 < np.std(output) < .15
    assert 48000 <= mixer.position <= 48480
    assert renderer.pending_frames < rate // 100


class FakeStream:
    def __init__(self, active, name, fail_start=False):
        self.active, self.name, self.fail_start = active, name, fail_start
        self.closed = False

    def start(self):
        if self.fail_start:
            raise RuntimeError("device unavailable")
        # Two callbacks must never drain the mixer/microphone queue together.
        assert not self.active
        self.active.add(self.name)

    def stop(self):
        self.active.discard(self.name)

    def close(self):
        self.stop()
        self.closed = True


@pytest.mark.parametrize("failure", [None, "open", "start"])
def test_audio_switch_is_transactional_and_never_runs_two_consumers(failure):
    active, streams = set(), {}

    def create(device):
        if device == 2 and failure == "open":
            raise RuntimeError("cannot open endpoint")
        streams[device] = FakeStream(active, device, failure == "start" and device == 2)
        return streams[device]

    stream = SwitchableStream(create, 1)
    stream.start()
    if failure:
        with pytest.raises(RuntimeError):
            stream.switch_device(2)
        assert active == {1} and stream.device == 1 and not streams[1].closed
        if failure == "start":
            assert streams[2].closed
    else:
        stream.switch_device(2)
        assert active == {2} and stream.device == 2 and streams[1].closed
    stream.close()
    assert not active
    with pytest.raises(RuntimeError, match="结束"):
        stream.switch_device(3)


async def test_receiver_switch_keeps_connections_and_mixed_audio(tmp_path):
    events, selected = [], []
    receiver = Receiver(Settings(), tmp_path, lambda kind, **data: events.append((kind, data)))
    receiver.output = SimpleNamespace(switch_device=lambda device: selected.append(device))
    connection = object()
    receiver.connections["phone"] = connection
    receiver.mixer.add("phone")
    import numpy as np
    receiver.mixer.write("phone", np.full((4000, 2), .2, dtype=np.float32))
    await receiver.set_audio_device("output", 7)
    assert selected == [7] and receiver.settings.output_device == 7
    assert receiver.connections["phone"] is connection
    assert receiver.mixer.inputs["phone"].frames == 4000


async def test_call_microphone_switch_keeps_voice_link_and_clears_old_capture(monkeypatch):
    import bluehead.calls as calls
    from .test_protocols import Network

    manager = calls.CallManager(Network(1).devices[0], Mixer(), lambda *args, **kwargs: None)
    selected = []
    manager.router.select("phone")
    manager.router.write_microphone("phone", b"old microphone")
    manager.links["phone"] = object()
    pending = manager.voice_buffers["phone"] = bytearray(b"old encoded audio")

    def switch(device):
        selected.append(device)
        manager.router.clear("phone")

    manager.microphone = SimpleNamespace(switch_device=switch, close=lambda: None)
    await manager.set_input_device(9)
    assert selected == [9] and manager.input_device == 9
    assert "phone" in manager.links and manager.router.owner == "phone"
    assert not pending and manager.router.read("phone", 4) == bytes(4)
    manager.close()


async def test_input_selection_does_not_open_microphone_without_call():
    from bluehead.calls import CallManager
    from .test_protocols import Network

    manager = CallManager(Network(1).devices[0], Mixer(), lambda *args, **kwargs: None)
    await manager.set_input_device(8)
    assert manager.input_device == 8 and manager.microphone is None
    manager.close()


async def test_stable_endpoint_identity_wins_over_stale_portaudio_index(monkeypatch, tmp_path):
    selected = []
    receiver = Receiver(Settings(), tmp_path, lambda *args, **kwargs: None)
    receiver.output = SimpleNamespace(switch_device=lambda device: selected.append(device))
    connection = receiver.connections["phone"] = object()
    monkeypatch.setattr("bluehead.endpoints.find_endpoint", lambda identity, direction: 18)
    actual = await receiver.set_audio_device("output", 7, "stable-endpoint")
    assert actual == 18 and selected == [18]
    assert receiver.settings.output_endpoint_id == "stable-endpoint"
    assert receiver.connections["phone"] is connection

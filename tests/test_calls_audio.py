from types import SimpleNamespace

import numpy as np
import pytest
from bumble import hci, hfp

from bluehead.audio import Mixer
from bluehead.calls import CallManager, Msbc
from .test_protocols import Network
from .vectors import signal


@pytest.mark.parametrize("wideband", [False, True])
@pytest.mark.parametrize("enhanced", [False, True])
async def test_actual_sco_accept_command_matches_host_codec(wideband, enhanced):
    commands = []

    async def send(command):
        # Round trip the actual HCI representation, not just Python kwargs.
        commands.append(hci.HCI_Command.from_bytes(bytes(command)))

    device = Network(1).devices[0]
    device.host.supports_command = lambda command: enhanced
    device.send_async_command = send
    manager = CallManager(device, Mixer(), lambda *args, **kwargs: None)
    protocol = SimpleNamespace(active_codec=hfp.AudioCodec.MSBC if wideband else hfp.AudioCodec.CVSD)
    await manager.accept_audio("phone", protocol, SimpleNamespace(peer_address=hci.Address("F0:F1:F2:F3:F4:F5")),
                               hci.HCI_Connection_Complete_Event.LinkType.ESCO)
    command = commands[-1]
    if enhanced:
        air_codec = hci.CodecID.TRANSPARENT if wideband else hci.CodecID.CVSD
        assert command.transmit_coding_format.codec_id == command.receive_coding_format.codec_id == air_codec
        host_codec = hci.CodecID.TRANSPARENT if wideband else hci.CodecID.LINEAR_PCM
        assert command.input_coding_format.codec_id == command.output_coding_format.codec_id == host_codec
        assert command.input_bandwidth == command.output_bandwidth == (8000 if wideband else 16000)
        assert command.input_data_path == command.output_data_path == 0
    else:
        assert command.voice_setting == (0x63 if wideband else 0x60)
    manager.close()


@pytest.mark.asyncio
async def test_preselected_microphone_mute_is_honored(tmp_path):
    from bluehead.backend import Receiver, Settings
    network = Network(1)
    receiver = Receiver(Settings(microphone_muted=True), tmp_path, lambda *args, **kwargs: None)
    receiver.attach(network.devices[0])
    assert receiver.calls.router.muted
    receiver.calls.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("wideband", [False, True])
@pytest.mark.parametrize("silent_downlink", [False, True])
async def test_duplex_sco_pcm_and_host_msbc_without_physical_microphone(monkeypatch, wideband, silent_downlink):
    # The virtual controller implements SCO signalling, not radio SCO payloads.
    # Exercise the exact production callback with correctly framed HCI SCO data.
    import bluehead.calls as calls
    network = Network(1)
    mixer = Mixer(buffer_ms=0, max_ms=500)
    emitted, uplink = [], []
    manager = CallManager(network.devices[0], mixer, lambda kind, **data: emitted.append((kind, data)))
    class SyntheticMic:
        def __init__(self, router, key, rate, device, emit):
            self.closed = False
            self.rate = rate
            self.feed = lambda: router.write_microphone(key, np.full(3000, 4000, dtype="<i2").tobytes())
            self.feed()
        def close(self):
            self.closed = True
    monkeypatch.setattr(calls, "Microphone", SyntheticMic)
    monkeypatch.setattr(manager.device.host, "send_sco_sdu", lambda handle, data: uplink.append(data))
    link = SimpleNamespace(handle=71, air_mode=hci.CodecID.TRANSPARENT if wideband else hci.CodecID.CVSD, once=lambda *args: None)
    protocol = SimpleNamespace(active_codec=hfp.AudioCodec.MSBC if wideband else hfp.AudioCodec.CVSD)
    try:
        # Recording apps can request a raw SCO link without CALL/CALL_SETUP indicators.
        assert not hasattr(protocol, "ag_indicators")
        manager.on_link("test-phone", protocol, link)
        await manager.capture_tasks["test-phone"]
        microphone = manager.microphone
        assert microphone is not None and mixer.call_active
        microphone.feed()
        source = signal(16000 if wideband else 8000, 0.15, channels=1).reshape(-1)
        if silent_downlink:
            source.fill(0)  # Input-only recording still gets microphone uplink.
        encoder, decoder = Msbc(), Msbc()
        decoded_mic = []
        for offset in range(0, len(source) - 120, 120):
            pcm = np.round(source[offset:offset + 120] * 32767).astype("<i2").tobytes()
            data = encoder.encode(pcm) if wideband else pcm[:60]
            packet = hci.HCI_SynchronousDataPacket(
                connection_handle=71, packet_status=hci.HCI_SynchronousDataPacket.Status.CORRECTLY_RECEIVED_DATA,
                data_total_length=len(data), data=data,
            )
            link.sink(packet)
            assert len(uplink[-1]) == len(data)
            decoded_mic.extend(decoder.decode(uplink[-1]) if wideband else [np.frombuffer(uplink[-1], "<i2").astype(np.float32) / 32768])
        rendered = mixer.render(480)
        assert np.max(np.abs(rendered)) < .001 if silent_downlink else np.std(rendered) > 0.005
        assert np.mean(np.concatenate(decoded_mic)[150:]) > 0.05
        assert manager.router.owner == "test-phone"
        assert manager.router.read("other-phone", 60) == bytes(60)
        manager.audio_closed("test-phone")
        await manager.wait_closed()
        assert manager.microphone is None and microphone.closed
        assert not mixer.call_active and manager.router.owner is None
    finally:
        manager.close()
        await manager.wait_closed()

"""HFP 1.9 signalling and native LC3-SWB on virtual controllers only."""

from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from bumble import core, hci, hfp, rfcomm, sdp

from bluehead.audio import Mixer
from bluehead.calls import CallManager
from bluehead.hfp_compat import (
    MAX_AT_BUFFER,
    MAX_AT_LINE,
    MAX_AT_QUEUE,
    HandsFreeProtocol,
    handsfree_records,
    renegotiate_fallback,
    selected_codec,
)
from bluehead.voice import VoiceCodec
from .test_protocols import Network


def configuration(codecs=(1, 2, 3)):
    return hfp.HfConfiguration(
        supported_hf_features=[hfp.HfFeature.CODEC_NEGOTIATION, hfp.HfFeature.ESCO_S4_SETTINGS_SUPPORTED],
        supported_hf_indicators=[],
        supported_audio_codecs=[hfp.AudioCodec(codec) for codec in codecs],
    )


def protocol(codecs=(1, 2, 3)):
    channel = SimpleNamespace(EVENT_CLOSE="close", on=lambda *args: None)
    dlc = SimpleNamespace(multiplexer=SimpleNamespace(l2cap_channel=channel))
    return HandsFreeProtocol(dlc, configuration(codecs))


def voice_link(air_mode=hci.CodecID.TRANSPARENT):
    listeners = {}
    link = SimpleNamespace(handle=71, air_mode=air_mode, disconnected=False)
    link.once = lambda name, callback: listeners.update({name: callback})
    link.listeners = listeners

    async def disconnect():
        link.disconnected = True
        if "disconnection" in listeners:
            listeners.pop("disconnection")(0)

    link.disconnect = disconnect
    return link


class SyntheticMicrophone:
    """Owned PCM producer; constructor never opens an OS endpoint."""

    def __init__(self, route, key, rate, device, emit):
        self.route, self.key, self.rate = route, key, rate
        self.closed = False
        self.frames = 0

    def feed(self):
        self.route.write_microphone(self.key, np.full(360, 5000, dtype="<i2").tobytes())
        self.frames += 360

    def switch_device(self, device):
        self.route.clear(self.key)

    def close(self):
        self.closed = True


@pytest.mark.parametrize("codecs,swb", [((1,), False), ((1, 2), False), ((1, 2, 3), True)])
def test_hfp19_sdp_has_exact_wideband_and_superwideband_bits(codecs, swb):
    records = handsfree_records(0x10002, 7, configuration(codecs))
    attributes = {attribute.id: attribute.value for attribute in records}
    descriptor = attributes[sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID]
    assert descriptor.value[0].value[1].value == 0x0109
    features = attributes[sdp.SDP_SUPPORTED_FEATURES_ATTRIBUTE_ID].value
    assert bool(features & int(hfp.HfSdpFeature.SUPER_WIDE_BAND)) == swb
    assert bool(features & int(hfp.HfSdpFeature.WIDE_BAND_SPEECH)) == (2 in codecs)


@pytest.mark.parametrize("enhanced", [False, True])
async def test_pending_lc3_is_used_for_esco_before_bcs_ok(monkeypatch, enhanced):
    import bluehead.calls as calls

    monkeypatch.setattr(calls, "Microphone", SyntheticMicrophone)
    manager = CallManager(Network(1).devices[0], Mixer(buffer_ms=0), lambda *args, **kwargs: None)
    manager.device.host.supports_command = lambda command: enhanced
    manager.device.host.send_sco_sdu = lambda *args: None
    commands = []

    async def send(command):
        commands.append(hci.HCI_Command.from_bytes(bytes(command)))

    manager.device.send_async_command = send
    hf = protocol()
    entered, ack = asyncio.Event(), asyncio.Event()

    async def execute(command):
        assert command == "AT+BCS=3"
        entered.set()
        await ack.wait()

    hf.execute_command = execute
    transaction = asyncio.create_task(hf.setup_codec_connection(3))
    try:
        await entered.wait()
        assert hf.active_codec == hfp.AudioCodec.CVSD
        assert selected_codec(hf) == hfp.AudioCodec.LC3_SWB
        await manager.accept_audio(
            "tablet",
            hf,
            SimpleNamespace(peer_address=hci.Address("F0:F1:F2:F3:F4:F5")),
            hci.HCI_Connection_Complete_Event.LinkType.ESCO,
        )
        command = commands[-1]
        if enhanced:
            assert command.input_coding_format.codec_id == hci.CodecID.TRANSPARENT
            assert command.receive_coding_format.codec_id == hci.CodecID.TRANSPARENT
            assert command.input_bandwidth == command.output_bandwidth == 8000
        else:
            assert command.voice_setting == 0x63
        link = voice_link()
        manager.on_link("tablet", hf, link)
        await manager.capture_tasks["tablet"]
        assert manager.voice_codecs["tablet"].codec_id == 3
        assert manager.microphone.rate == 48000
        ack.set()
        await transaction
        assert hf.pending_codec is None
        assert hf.active_codec == hfp.AudioCodec.LC3_SWB
    finally:
        ack.set()
        transaction.cancel()
        await asyncio.gather(transaction, return_exceptions=True)
        manager.close()
        await manager.wait_closed()


@pytest.mark.parametrize("failure", [RuntimeError("AG returned ERROR"), asyncio.CancelledError()])
async def test_bcs_failure_clears_pending_codec_and_reports_transaction(failure):
    hf = protocol()
    failures = []
    hf.on("codec_failed", failures.append)

    async def execute(command):
        assert hf.audio_codec == hfp.AudioCodec.LC3_SWB
        raise failure

    hf.execute_command = execute
    with pytest.raises(type(failure)):
        await hf.setup_codec_connection(3)
    assert hf.pending_codec is None
    assert hf.active_codec == hfp.AudioCodec.CVSD
    assert failures == [hfp.AudioCodec.LC3_SWB]


async def test_unsupported_bcs_replies_with_bac_without_claiming_codec():
    hf = protocol((1, 2))
    commands = []

    async def execute(command):
        commands.append(command)

    hf.execute_command = execute
    await hf.setup_codec_connection(3)
    assert commands == ["AT+BAC=1,2"]
    assert hf.pending_codec is None and hf.active_codec == hfp.AudioCodec.CVSD


@pytest.mark.parametrize("codec", [hfp.AudioCodec.MSBC, hfp.AudioCodec.LC3_SWB])
async def test_compressed_codec_rejects_sco_without_local_codec_fallback(codec):
    manager = CallManager(Network(1).devices[0], Mixer(), lambda *args, **kwargs: None)
    commands = []

    async def send(command):
        commands.append(hci.HCI_Command.from_bytes(bytes(command)))

    manager.device.send_async_command = send
    hf = SimpleNamespace(active_codec=codec)
    try:
        await manager.accept_audio(
            "tablet",
            hf,
            SimpleNamespace(peer_address=hci.Address("F0:F1:F2:F3:F4:F5")),
            hci.HCI_Connection_Complete_Event.LinkType.SCO,
        )
        assert isinstance(commands[-1], hci.HCI_Reject_Synchronous_Connection_Request_Command)
        assert hf.active_codec == codec
        assert not manager.links and not manager.voice_codecs
    finally:
        manager.close()
        await manager.wait_closed()


@pytest.mark.parametrize(
    "codec,air_mode",
    [
        (1, hci.CodecID.TRANSPARENT),
        (1, hci.CodecID.A_LOG),
        (1, hci.CodecID.U_LOG),
        (2, hci.CodecID.CVSD),
        (3, hci.CodecID.CVSD),
    ],
)
async def test_link_air_mode_must_match_negotiated_codec(monkeypatch, codec, air_mode):
    import bluehead.calls as calls

    monkeypatch.setattr(calls, "Microphone", SyntheticMicrophone)
    manager = CallManager(Network(1).devices[0], Mixer(), lambda *args, **kwargs: None)
    manager.device.host.send_sco_sdu = lambda *args: None
    link = voice_link(air_mode)
    try:
        manager.on_link("tablet", SimpleNamespace(active_codec=hfp.AudioCodec(codec)), link)
        await asyncio.sleep(0)
        assert link.disconnected
        assert not manager.links and not manager.voice_codecs
        assert not manager.capture_tasks and manager.microphone is None
    finally:
        manager.close()
        await manager.wait_closed()


@pytest.mark.parametrize("recording_only", [False, True])
async def test_virtual_lc3_duplex_plc_and_recording_without_call_indicators(monkeypatch, recording_only):
    import bluehead.calls as calls

    monkeypatch.setattr(calls, "Microphone", SyntheticMicrophone)
    mixer = Mixer(buffer_ms=0, max_ms=500)
    manager = CallManager(Network(1).devices[0], mixer, lambda *args, **kwargs: None)
    uplink = []
    manager.device.host.send_sco_sdu = lambda handle, data: uplink.append(bytes(data))
    link = voice_link()
    hf = SimpleNamespace(active_codec=hfp.AudioCodec.LC3_SWB)
    remote_encoder, remote_decoder = VoiceCodec(3), VoiceCodec(3)
    downlink_pcm, microphone_pcm = [], []
    try:
        assert not hasattr(hf, "ag_indicators")
        manager.on_link("tablet", hf, link)
        await manager.capture_tasks["tablet"]
        microphone = manager.microphone
        for index in range(32):
            microphone.feed()
            tone = np.rint(9000 * np.sin(2 * np.pi * 701 * (np.arange(360) + index * 360) / 48000))
            pcm = bytes(720) if recording_only else tone.astype("<i2").tobytes()
            packet = remote_encoder.encode(pcm)
            for fragment in (packet[:33], packet[33:]):
                link.sink(
                    hci.HCI_SynchronousDataPacket(
                        connection_handle=71,
                        packet_status=1 if index == 12 else 0,
                        data_total_length=len(fragment),
                        data=fragment,
                    )
                )
                assert len(uplink[-1]) == len(fragment)
            downlink_pcm.append(mixer.render(360))
        for fragment in uplink:
            remote_decoder.push_rx(fragment)
            microphone_pcm.append(np.frombuffer(remote_decoder.read_pcm(), dtype="<i2"))
        rendered = np.concatenate(downlink_pcm)
        upstream = np.concatenate(microphone_pcm).astype(np.float64)[1080:]
        if recording_only:
            assert np.max(np.abs(rendered)) < 0.001
        else:
            assert np.std(rendered) > 0.01
        assert upstream.mean() > 3000
        stats = manager.voice_codecs["tablet"].stats()
        assert stats["corrupt_frames"] == stats["plc_frames"] == 1
        assert manager.statistics["tablet"]["rate"] == 32000
        assert manager.statistics["tablet"]["pcm_rate"] == 48000
        assert manager.statistics["tablet"]["decode_errors"] == 0
        old_sink, codec = link.sink, manager.voice_codecs["tablet"]
        await link.disconnect()
        await manager.wait_closed()
        assert microphone.closed and manager.microphone is None
        assert manager.router.owner is None and not mixer.call_active
        assert not manager.links and not manager.voice_codecs and not manager.voice_buffers
        assert codec.handle is None
        old_sink(
            hci.HCI_SynchronousDataPacket(connection_handle=71, packet_status=0, data_total_length=60, data=packet)
        )
        assert manager.statistics["tablet"]["decode_errors"] == 0
    finally:
        remote_encoder.close()
        remote_decoder.close()
        manager.close()
        await manager.wait_closed()


async def test_real_rfcomm_slc_advertises_lc3_and_negotiates_back_to_cvsd():
    network = Network(2)
    manager = CallManager(network.devices[0], Mixer(), lambda *args, **kwargs: None)
    await network.start()
    try:
        async with asyncio.timeout(10):
            connection = await network.devices[1].connect(
                network.devices[0].public_address, transport=core.PhysicalTransport.BR_EDR
            )
            mux = await rfcomm.Client(connection).start()
            dlc = await mux.open_dlc(next(iter(manager.server.acceptors)))
            ag = hfp.AgProtocol(
                dlc,
                hfp.AgConfiguration(
                    supported_ag_features=[
                        hfp.AgFeature.CODEC_NEGOTIATION,
                        hfp.AgFeature.ESCO_S4_SETTINGS_SUPPORTED,
                    ],
                    supported_ag_indicators=[hfp.AgIndicatorState.call(), hfp.AgIndicatorState.callsetup()],
                    supported_hf_indicators=[],
                    supported_ag_call_hold_operations=[],
                    supported_audio_codecs=[hfp.AudioCodec.CVSD, hfp.AudioCodec.MSBC, hfp.AudioCodec.LC3_SWB],
                ),
            )
            while not manager.protocols or not next(iter(manager.protocols.values()))._slc_initialized:
                await asyncio.sleep(0.005)
            hf = next(iter(manager.protocols.values()))
            assert set(ag.supported_audio_codecs) == {
                hfp.AudioCodec.CVSD,
                hfp.AudioCodec.MSBC,
                hfp.AudioCodec.LC3_SWB,
            }
            for codec in (hfp.AudioCodec.LC3_SWB, hfp.AudioCodec.MSBC, hfp.AudioCodec.CVSD):
                event = asyncio.Event()
                hf.once("codec_negotiation", lambda result: event.set())
                await ag.negotiate_codec(codec)
                await event.wait()
                assert hf.active_codec == codec and hf.pending_codec is None
            assert manager.microphone is None
            await connection.disconnect()
            await asyncio.sleep(0.01)
            assert not manager.protocols
    finally:
        manager.close()
        await manager.wait_closed()
        await network.close()


def test_wait_closed_yields_to_pending_done_callback_in_subprocess():
    # A subprocess timeout also catches a non-yielding busy loop, which an
    # asyncio timeout on the affected event loop cannot interrupt.
    program = """
import asyncio
from bumble.device import Device
from bumble.host import Host
from bluehead.audio import Mixer
from bluehead.calls import CallManager
async def run():
    manager = CallManager(Device(host=Host()), Mixer(), lambda *args, **kwargs: None)
    future = asyncio.get_running_loop().create_future()
    manager.tasks.add(future)
    future.add_done_callback(manager.tasks.discard)
    future.set_result(None)
    manager.close()
    await manager.wait_closed()
    assert not manager.tasks
asyncio.run(run())
print('cleanup completed')
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0 and "cleanup completed" in result.stdout, result.stderr


@pytest.mark.parametrize("value", [0, 1, 7, 15])
async def test_hfp_standard_speaker_and_microphone_gains_multiply_software_gain(value):
    mixer = Mixer(buffer_ms=0)
    manager = CallManager(Network(1).devices[0], mixer, lambda *args, **kwargs: None)
    try:
        mixer.add("call:tablet")
        manager.set_downlink_gain("tablet", 2.0)
        manager.remote_volume("tablet", value, "speaker")
        mixer.write_voice("call:tablet", np.full(360, 4000, dtype="<i2").tobytes())
        actual = mixer.render(360)
        np.testing.assert_allclose(actual, (4000 / 32768) * 2 * value / 15, atol=2e-6)
        manager.router.configure(48000)
        manager.router.select("tablet")
        manager.router.device_gains["tablet"] = 2.0
        manager.remote_volume("tablet", value, "microphone")
        manager.router.write_microphone("tablet", np.full(360, 4000, dtype="<i2").tobytes())
        upstream = np.frombuffer(manager.router.read("tablet", 720), dtype="<i2")
        np.testing.assert_allclose(upstream, 4000 * 2 * value / 15, atol=1)
        assert "other" not in manager.speaker_volumes
        assert "other" not in manager.router.device_standard_gains
    finally:
        manager.close()
        await manager.wait_closed()


@pytest.mark.parametrize("value", [-1, 16, 128, 1.5, "7", None, True])
async def test_invalid_hfp_remote_gain_is_ignored(value):
    events = []
    manager = CallManager(Network(1).devices[0], Mixer(), lambda *args, **kwargs: events.append((args, kwargs)))
    try:
        manager.remote_volume("tablet", 7, "speaker")
        manager.remote_volume("tablet", 9, "microphone")
        before = dict(manager.speaker_volumes), dict(manager.router.device_standard_gains), len(events)
        manager.remote_volume("tablet", value, "speaker")
        manager.remote_volume("tablet", value, "microphone")
        assert before == (manager.speaker_volumes, manager.router.device_standard_gains, len(events))
    finally:
        manager.close()
        await manager.wait_closed()


@asynccontextmanager
async def fallback_peers(count=1):
    """Real virtual RFCOMM/AT state machines without an audio endpoint."""
    network = Network(count + 1)
    peers, tasks, connections = [], [], []

    def accept(dlc):
        hf = HandsFreeProtocol(dlc, configuration())
        peers.append(hf)
        tasks.append(asyncio.create_task(hf.run()))

    server = rfcomm.Server(network.devices[0])
    channel = server.listen(accept)
    await network.start()
    try:
        async with asyncio.timeout(10):
            pairs = []
            for device in network.devices[1:]:
                connection = await device.connect(
                    network.devices[0].public_address, transport=core.PhysicalTransport.BR_EDR
                )
                connections.append(connection)
                mux = await rfcomm.Client(connection).start()
                dlc = await mux.open_dlc(channel)
                ag = hfp.AgProtocol(
                    dlc,
                    hfp.AgConfiguration(
                        supported_ag_features=[hfp.AgFeature.CODEC_NEGOTIATION],
                        supported_ag_indicators=[hfp.AgIndicatorState.call(), hfp.AgIndicatorState.callsetup()],
                        supported_hf_indicators=[],
                        supported_ag_call_hold_operations=[],
                        supported_audio_codecs=list(hfp.AudioCodec),
                    ),
                )
                while len(peers) < len(pairs) + 1 or not peers[-1]._slc_initialized:
                    await asyncio.sleep(0.001)
                hf = peers[-1]
                await ag.negotiate_codec(hfp.AudioCodec.LC3_SWB)
                while hf.active_codec != hfp.AudioCodec.LC3_SWB:
                    await asyncio.sleep(0)
                pairs.append((hf, ag))
            yield pairs
    finally:
        for connection in connections:
            await connection.disconnect()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await network.close()


async def test_fallback_uses_real_bac_bcc_bcs_for_lc3_msbc_cvsd():
    async with fallback_peers() as [(hf, ag)]:
        wire = []
        old_bac, old_bcc = ag._on_bac, ag._on_bcc
        ag._on_bac = lambda *args: (wire.append(("BAC", tuple(int(arg) for arg in args))), old_bac(*args))
        ag._on_bcc = lambda: (wire.append(("BCC",)), old_bcc())
        for failed, selected, offered in [(3, 2, (1, 2)), (2, 1, (1,))]:
            result = await renegotiate_fallback(hf)
            assert result.status == "requested" and result.requested
            assert result.failed_codec == failed and result.offered_codecs == offered
            assert not result.reconnect_required
            assert wire[-2:] == [("BAC", offered), ("BCC",)]
            # BAC/BCC acceptance is not codec selection or an audio connection.
            assert hf.active_codec == ag.active_codec == failed
            assert tuple(ag.supported_audio_codecs) == offered
            await ag.negotiate_codec(hfp.AudioCodec(selected))
            while hf.active_codec != selected:
                await asyncio.sleep(0)
        before = list(wire)
        result = await renegotiate_fallback(hf)
        assert result.status == "exhausted" and not result.requested
        assert wire == before and hf.active_codec == hfp.AudioCodec.CVSD


async def test_fallback_publishes_offer_before_immediate_bcs_response():
    async with fallback_peers() as [(hf, ag)]:
        old_bac = ag._on_bac
        observed = []

        def bac(*args):
            observed.append(tuple(hf.supported_audio_codecs))
            ag.send_response("+BCS: 2")
            old_bac(*args)

        ag._on_bac = bac
        result = await renegotiate_fallback(hf)
        while hf.active_codec != hfp.AudioCodec.MSBC:
            await asyncio.sleep(0)
        assert result.requested and observed == [(1, 2)]
        assert hf.pending_codec is None and ag.active_codec == hfp.AudioCodec.MSBC


@pytest.mark.parametrize("failed_command", ["BAC", "BCC"])
async def test_fallback_at_error_restores_local_and_remote_offer(failed_command):
    async with fallback_peers() as [(hf, ag)]:
        wire = []
        old_bac, old_bcc = ag._on_bac, ag._on_bcc

        def bac(*args):
            wire.append(("BAC", tuple(int(arg) for arg in args)))
            if failed_command == "BAC":
                ag.send_error()
            else:
                old_bac(*args)

        ag._on_bac = bac
        ag._on_bcc = lambda: (wire.append(("BCC",)), ag.send_error())
        result = await renegotiate_fallback(hf)
        assert result.status == "rejected" and not result.reconnect_required
        assert tuple(hf.supported_audio_codecs) == tuple(ag.supported_audio_codecs) == (1, 2, 3)
        assert hf.active_codec == ag.active_codec == hfp.AudioCodec.LC3_SWB
        if failed_command == "BCC":
            assert wire == [("BAC", (1, 2)), ("BCC",), ("BAC", (1, 2, 3))]
        else:
            assert wire == [("BAC", (1, 2))]
        # A rejected request is not retried indefinitely on repeated HCI events.
        assert (await renegotiate_fallback(hf)).status == "exhausted"
        ag._on_bac, ag._on_bcc = old_bac, old_bcc


async def test_fallback_does_not_withdraw_another_peers_capabilities():
    async with fallback_peers(2) as [(hf_a, ag_a), (hf_b, ag_b)]:
        assert (await renegotiate_fallback(hf_a)).requested
        assert hf_a.supported_audio_codecs == ag_a.supported_audio_codecs == [1, 2]
        assert hf_b.supported_audio_codecs == ag_b.supported_audio_codecs == [1, 2, 3]
        assert hf_b.active_codec == hfp.AudioCodec.LC3_SWB
        assert (await renegotiate_fallback(hf_b)).requested


async def test_fallback_duplicate_and_overlapping_failures_are_bounded():
    async with fallback_peers() as [(hf, ag)]:
        bac_received = asyncio.Event()
        ag._on_bac = lambda *args: bac_received.set()
        task = asyncio.create_task(renegotiate_fallback(hf))
        await bac_received.wait()
        assert (await renegotiate_fallback(hf)).status == "busy"
        ag.send_ok()
        result = await task
        assert result.requested
        # BCS has not selected another codec: another old-LC3 failure must not
        # skip mSBC and silently reduce the offer a second time.
        assert (await renegotiate_fallback(hf)).status == "exhausted"
        assert hf.supported_audio_codecs == [1, 2] and hf.active_codec == 3


@pytest.mark.parametrize("cancel", [False, True])
async def test_fallback_timeout_or_cancellation_does_not_send_ambiguous_cleanup_at(cancel):
    async with fallback_peers() as [(hf, ag)]:
        wire, received = [], asyncio.Event()

        def bac(*args):
            wire.append(tuple(int(arg) for arg in args))
            received.set()

        ag._on_bac = bac  # Deliberately leave the BAC command unanswered.
        task = asyncio.create_task(renegotiate_fallback(hf, timeout=0.04))
        await received.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.status == "timeout" and result.reconnect_required
        assert wire == [(1, 2)]
        assert hf.supported_audio_codecs == [1, 2, 3]
        assert hf.active_codec == hfp.AudioCodec.LC3_SWB


async def test_fallback_failed_capability_restoration_requires_new_slc():
    async with fallback_peers() as [(hf, ag)]:
        old_bac = ag._on_bac

        def bac(*args):
            if len(args) == 2:
                old_bac(*args)
            else:
                ag.send_error()

        ag._on_bac = bac
        ag._on_bcc = ag.send_error
        result = await renegotiate_fallback(hf)
        assert result.status == "rejected" and result.reconnect_required
        assert hf.supported_audio_codecs == [1, 2, 3]
        assert ag.supported_audio_codecs == [1, 2]


async def test_fallback_without_negotiation_support_does_not_issue_at_commands():
    hf = protocol()
    hf.active_codec = hfp.AudioCodec.LC3_SWB
    result = await renegotiate_fallback(hf)
    assert result.status == "exhausted" and hf.supported_audio_codecs == [1, 2, 3]


async def test_delayed_old_codec_failure_does_not_downgrade_new_bcs_selection():
    async with fallback_peers() as [(hf, ag)]:
        await ag.negotiate_codec(hfp.AudioCodec.MSBC)
        while hf.active_codec != hfp.AudioCodec.MSBC:
            await asyncio.sleep(0)
        wire = []
        ag._on_bac = lambda *args: wire.append(args)
        ag._on_bcc = lambda: wire.append("BCC")
        result = await renegotiate_fallback(hf, failed_codec=hfp.AudioCodec.LC3_SWB)
        assert result.status == "stale" and result.failed_codec == hfp.AudioCodec.LC3_SWB
        assert not result.requested and not result.reconnect_required
        assert not wire and hf.active_codec == hfp.AudioCodec.MSBC
        assert hf.supported_audio_codecs == [1, 2, 3]


@pytest.mark.parametrize("split", [1, 2, 3, 7, 127])
async def test_at_framing_keeps_every_legal_fragment_and_shared_crlf(split):
    hf = protocol()
    wire = b"\r\nRING\r\n\r\n+VGS: 7\r\n+VGM: 12\r\n"
    errors = []
    hf.on("protocol_error", errors.append)
    for start in range(0, len(wire), split):
        hf._read_at(wire[start : start + split])
    responses = [hf.unsolicited_queue.get_nowait() for _ in range(3)]
    assert [response.code for response in responses] == ["RING", "+VGS", "+VGM"]
    assert responses[1].parameters == [b"7"] and responses[2].parameters == [b"12"]
    assert not hf.stopped and not errors and not hf.read_buffer


async def test_maximum_valid_at_line_and_split_delimiter_survive():
    hf = protocol()
    hf.pending_command = "AT+CIND?"
    payload = b"+CIND: " + b"1" * (MAX_AT_LINE - 7)
    assert len(payload) == MAX_AT_LINE
    hf._read_at(b"\r\n" + payload + b"\r")
    assert not hf.stopped
    hf._read_at(b"\n")
    result = hf.response_queue.get_nowait()
    assert result.code == "+CIND" and len(result.parameters[0]) == MAX_AT_LINE - 7
    assert not hf.read_buffer and not hf.stopped


@pytest.mark.parametrize(
    "wire,reason",
    [
        (b"x" * (MAX_AT_BUFFER + 1), "buffer limit"),
        (b"\r\n" + b"x" * (MAX_AT_LINE + 1), "line limit"),
        (b"\r\n" + b"x" * (MAX_AT_LINE + 1) + b"\r\n", "line limit"),
        (b"\r\n+VGS: )\r\n", "malformed"),
        (b"\r\n\xff: 1\r\n", "malformed"),
        (b"\r\n: 1\r\n", "malformed"),
    ],
)
async def test_malformed_at_is_consumed_and_aborts_only_this_peer(wire, reason, caplog):
    bad, healthy = protocol(), protocol()
    errors = []
    bad.on("protocol_error", errors.append)
    bad._read_at(wire)
    bad._read_at(b"\r\n+CLIP: \"+15555550123\",129\r\n")
    bad.abort("unknown caller-supplied sensitive text")
    healthy._read_at(b"\r\nRING\r\n")
    assert bad.stopped and not healthy.stopped
    assert not bad.read_buffer and len(errors) == 1 and reason in errors[0]
    assert bad.unsolicited_queue.get_nowait() is None
    assert healthy.unsolicited_queue.get_nowait().code == "RING"
    assert "+15555550123" not in caplog.text and "sensitive text" not in str(errors)


@pytest.mark.parametrize("response_queue", [False, True])
async def test_at_queue_flood_is_bounded_and_wakes_waiters(response_queue):
    hf = protocol()
    errors = []
    hf.on("protocol_error", errors.append)
    if response_queue:
        hf.pending_command = "AT+BCC"
    packet = b"\r\nOK\r\n" if response_queue else b"\r\nRING\r\n"
    for _ in range(MAX_AT_QUEUE):
        hf._read_at(packet)
    queue = hf.response_queue if response_queue else hf.unsolicited_queue
    assert queue.qsize() == MAX_AT_QUEUE and not hf.stopped
    hf._read_at(packet)
    expected = "HFP command response limit exceeded" if response_queue else "HFP response queue limit exceeded"
    assert hf.stopped and errors == [expected]
    assert hf.response_queue.qsize() == hf.unsolicited_queue.qsize() == 1
    assert hf.response_queue.get_nowait().code == "ERROR"
    assert hf.unsolicited_queue.get_nowait() is None


async def test_stop_prevents_pending_command_waiters_sending_after_close():
    hf = protocol()
    commands = []
    hf.dlc.write = commands.append
    tasks = [asyncio.create_task(hf.execute_command(command)) for command in ("AT+BCC", "AT+BAC=1")]
    await asyncio.sleep(0)
    assert commands == ["AT+BCC\r"]
    assert hf.stop() and not hf.stop()
    results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 0.5)
    assert all(isinstance(result, hfp.HfpProtocolError) for result in results)
    assert commands == ["AT+BCC\r"]


async def test_l2cap_close_sentinel_can_wake_a_completely_full_queue():
    hf = protocol()
    for _ in range(MAX_AT_QUEUE):
        hf.unsolicited_queue.put_nowait(hfp.AtResponse("RING", []))
    # This is the exact callback registered by the upstream HfProtocol.
    hf.unsolicited_queue.put_nowait(None)
    assert hf.unsolicited_queue.qsize() == MAX_AT_QUEUE
    hf._slc_initialized = True
    await asyncio.wait_for(hf.run(), 0.5)
    assert hf.stopped


async def test_dispatch_parse_error_closes_without_logging_raw_phone_data(caplog):
    caplog.set_level("DEBUG")
    hf = protocol()
    hf._slc_initialized = True
    errors = []
    hf.on("protocol_error", errors.append)
    hf._read_at(b"\r\n+VGS: \"caller +15555550123\"\r\n")
    await asyncio.wait_for(hf.run(), 0.5)
    assert hf.stopped and errors == ["HFP response is malformed"]
    assert "+15555550123" not in caplog.text


async def test_unknown_vendor_result_is_ignored_without_forwarding_its_raw_text(caplog):
    caplog.set_level("DEBUG")
    hf = protocol()
    hf._read_at(b"\r\nPRIVATE +15555550123: 1\r\n")
    assert not hf.stopped and hf.unsolicited_queue.empty() and not hf.read_buffer
    assert "+15555550123" not in caplog.text


async def test_real_rfcomm_peer_survives_other_peer_malformed_at():
    async with fallback_peers(2) as [(bad, ag_bad), (healthy, ag_healthy)]:
        errors = []
        bad.on("protocol_error", errors.append)
        ag_bad.dlc.write(b"\r\n+VGS: )\r\n")
        while not bad.stopped:
            await asyncio.sleep(0)
        await ag_healthy.negotiate_codec(hfp.AudioCodec.MSBC)
        while healthy.active_codec != hfp.AudioCodec.MSBC:
            await asyncio.sleep(0)
        assert len(errors) == 1 and not healthy.stopped


@pytest.mark.parametrize(
    "parameters",
    [
        b'("call",(0-999999999))',
        b'("call",(0-65535))',
        b'("call",(100-1))',
        b'("call",(0-255,0-255))',
        b'("call",0)',
        b",".join([b'("call",(0,1))'] * 33),
    ],
)
async def test_cind_ranges_cannot_expand_bounded_wire_into_unbounded_heap(parameters):
    hf = protocol()
    hf.pending_command = "AT+CIND=?"
    hf._read_at(b"\r\n+CIND: " + parameters + b"\r\n")
    assert hf.stopped and not hf.read_buffer
    assert hf.response_queue.qsize() == 1 and hf.response_queue.get_nowait().code == "ERROR"


async def test_cind_standard_and_vendor_ranges_remain_compatible():
    hf = protocol()
    hf.pending_command = "AT+CIND=?"
    hf._read_at(b'\r\n+CIND: ("call",(0,1)),("battery",(0-100))\r\n')
    assert not hf.stopped
    result = hf.response_queue.get_nowait()
    assert result.code == "+CIND" and len(result.parameters) == 2


async def test_total_command_response_count_is_bounded_even_when_queue_is_drained():
    hf = protocol()
    hf.dlc.write = lambda command: None
    errors = []
    hf.on("protocol_error", errors.append)
    task = asyncio.create_task(hf.execute_command("AT+CIND?", response_type=hfp.AtResponseType.MULTIPLE))
    await asyncio.sleep(0)
    for _ in range(MAX_AT_QUEUE + 1):
        hf._read_at(b"\r\n+CIND: 0,1\r\n")
        await asyncio.sleep(0)
    with pytest.raises(hfp.HfpProtocolError):
        await task
    assert hf.stopped and errors == ["HFP command response limit exceeded"]
    assert hf.response_queue.qsize() <= 1


async def test_total_command_deadline_cannot_be_extended_by_partial_responses():
    hf = protocol()
    hf.dlc.write = lambda command: None
    task = asyncio.create_task(hf.execute_command("AT+CIND?", timeout=0.06, response_type=hfp.AtResponseType.MULTIPLE))
    for _ in range(15):
        await asyncio.sleep(0.01)
        if task.done():
            break
        hf._read_at(b"\r\n+CIND: 0,1\r\n")
    with pytest.raises(TimeoutError):
        await task
    assert hf.stopped


async def test_cancelled_command_late_ok_cannot_acknowledge_a_new_command():
    hf = protocol()
    commands = []
    hf.dlc.write = commands.append
    task = asyncio.create_task(hf.execute_command("AT+BCC"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    hf._read_at(b"\r\nOK\r\n")
    with pytest.raises(hfp.HfpProtocolError):
        await hf.execute_command("AT+BAC=1")
    assert commands == ["AT+BCC\r"] and hf.stopped

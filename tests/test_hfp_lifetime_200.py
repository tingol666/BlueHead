"""Call-manager RFCOMM ownership and fallback; virtual controllers only."""

import asyncio
from contextlib import asynccontextmanager
import threading
from types import SimpleNamespace

import pytest
from bumble import core, hci, hfp, rfcomm

from bluehead.audio import Mixer
from bluehead.calls import CallManager
from bluehead.voice import VoiceCodec
from .test_hfp_compat import SyntheticMicrophone, voice_link
from .test_protocols import Network


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("stage,cancel", [("start", False), ("open", False), ("start", True), ("open", True)])
async def test_partial_outbound_rfcomm_failure_or_cancellation_disposes_owned_client(monkeypatch, stage, cancel):
    import bluehead.calls as calls

    instances, entered = [], asyncio.Event()

    class PartialClient:
        def __init__(self, connection):
            self.multiplexer = SimpleNamespace(open_dlc=self.open_dlc)
            self.shutdown_calls = 0
            instances.append(self)

        async def fail(self):
            entered.set()
            if cancel:
                await asyncio.Future()
            raise RuntimeError("peer refused RFCOMM")

        async def start(self):
            if stage == "start":
                await self.fail()
            return self.multiplexer

        async def open_dlc(self, channel):
            await self.fail()

        async def shutdown(self):
            self.shutdown_calls += 1

    async def find(connection):
        return (7, 0)

    monkeypatch.setattr(calls.rfcomm, "Client", PartialClient)
    monkeypatch.setattr(calls.hfp, "find_ag_sdp_record", find)
    manager = CallManager(Network(1).devices[0], Mixer(), lambda *args, **kwargs: None)
    manager.connections["peer"] = SimpleNamespace()
    task = asyncio.create_task(manager.ensure_control("peer"))
    try:
        await entered.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await task
        await manager.wait_closed()
        assert len(instances) == 1 and instances[0].shutdown_calls == 1
        assert "peer" not in manager.clients and "peer" not in manager.protocols
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        manager.close()
        await manager.wait_closed()


async def test_failed_mux_shutdown_still_closes_owned_l2cap_channel():
    closed = []

    async def fail_mux():
        raise TimeoutError("peer did not acknowledge mux DISC")

    async def close_l2cap():
        closed.append(True)

    manager = CallManager(Network(1).devices[0], Mixer(), lambda *args, **kwargs: None)
    client = rfcomm.Client(SimpleNamespace())
    client.multiplexer = SimpleNamespace(disconnect=fail_mux)
    client.l2cap_channel = SimpleNamespace(disconnect=close_l2cap)
    try:
        manager.retire_rfcomm_client(client)
        await asyncio.wait_for(manager.wait_closed(), 1)
        assert closed == [True]
    finally:
        manager.close()
        await manager.wait_closed()


async def test_client_start_with_l2cap_but_no_mux_still_releases_channel():
    closed = []

    async def close_l2cap():
        closed.append(True)

    manager = CallManager(Network(1).devices[0], Mixer(), lambda *args, **kwargs: None)
    client = rfcomm.Client(SimpleNamespace())
    client.l2cap_channel = SimpleNamespace(disconnect=close_l2cap)
    try:
        manager.retire_rfcomm_client(client)
        await asyncio.wait_for(manager.wait_closed(), 1)
        assert closed == [True]
    finally:
        manager.close()
        await manager.wait_closed()


async def test_manager_close_during_dlc_disconnect_releases_its_channel():
    entered, closed = asyncio.Event(), []

    async def stuck_dlc():
        entered.set()
        await asyncio.Future()

    async def close_l2cap():
        closed.append(True)

    manager = CallManager(Network(1).devices[0], Mixer(), lambda *args, **kwargs: None)
    dlc = SimpleNamespace(
        disconnect=stuck_dlc,
        multiplexer=SimpleNamespace(l2cap_channel=SimpleNamespace(disconnect=close_l2cap)),
    )
    try:
        manager.spawn(manager.disconnect_dlc(dlc))
        await entered.wait()
        manager.close()
        await asyncio.wait_for(manager.wait_closed(), 1)
        assert closed == [True]
    finally:
        manager.close()
        await manager.wait_closed()


async def test_incoming_slc_wins_during_outbound_open_without_orphaning_client(monkeypatch):
    import bluehead.calls as calls

    entered, release = asyncio.Event(), asyncio.Event()
    instances = []
    connection = SimpleNamespace(peer_address="peer")
    channel = SimpleNamespace(connection=connection)
    manager = CallManager(Network(1).devices[0], Mixer(), lambda *args, **kwargs: None)

    class Client:
        def __init__(self, connection):
            self.shutdown_calls = 0
            self.multiplexer = SimpleNamespace(open_dlc=self.open_dlc, l2cap_channel=channel)
            instances.append(self)

        async def start(self):
            return self.multiplexer

        async def open_dlc(self, number):
            entered.set()
            await release.wait()

            async def disconnect():
                pass

            return SimpleNamespace(multiplexer=self.multiplexer, disconnect=disconnect)

        async def shutdown(self):
            self.shutdown_calls += 1

    async def find(connection):
        return (7, 0)

    monkeypatch.setattr(calls.rfcomm, "Client", Client)
    monkeypatch.setattr(calls.hfp, "find_ag_sdp_record", find)
    manager.connections["peer"] = connection
    task = asyncio.create_task(manager.ensure_control("peer"))
    try:
        await entered.wait()
        incoming = SimpleNamespace(_slc_initialized=True)
        manager.protocols["peer"] = incoming
        manager.control_ready["peer"] = asyncio.Event()
        manager.control_ready["peer"].set()
        release.set()
        assert await task is incoming
        await manager.wait_closed()
        assert "peer" not in manager.clients and instances[0].shutdown_calls == 1
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        manager.close()
        await manager.wait_closed()


@asynccontextmanager
async def managed_control(monkeypatch):
    network = Network(2)
    events, gateways = [], []
    manager = CallManager(network.devices[0], Mixer(), lambda kind, **data: events.append((kind, data)))

    def accept(dlc):
        gateways.append(
            hfp.AgProtocol(
                dlc,
                hfp.AgConfiguration(
                    supported_ag_features=[hfp.AgFeature.CODEC_NEGOTIATION],
                    supported_ag_indicators=[hfp.AgIndicatorState.call(), hfp.AgIndicatorState.callsetup()],
                    supported_hf_indicators=[],
                    supported_ag_call_hold_operations=[],
                    supported_audio_codecs=list(hfp.AudioCodec),
                ),
            )
        )

    server = rfcomm.Server(network.devices[1])
    channel = server.listen(accept)

    async def find(connection):
        return (channel, 0)

    monkeypatch.setattr(hfp, "find_ag_sdp_record", find)
    await network.start()
    try:
        connection = await network.devices[0].connect(
            network.devices[1].public_address, transport=core.PhysicalTransport.BR_EDR
        )
        key = str(connection.peer_address)
        protocol = await manager.ensure_control(key)
        yield manager, connection, key, protocol, gateways, events
    finally:
        manager.close()
        await asyncio.wait_for(manager.wait_closed(), 3)
        await network.close()


async def test_dlc_close_without_acl_disconnect_releases_client_and_allows_fresh_slc(monkeypatch):
    async with managed_control(monkeypatch) as (manager, connection, key, hf, gateways, events):
        client = manager.clients[key]
        await hf.dlc.disconnect()
        await until(lambda: key not in manager.protocols and key not in manager.clients and not manager.cleanup_tasks)
        assert hf.stopped and manager.connections[key] is connection
        # A simultaneous DLC/run-finally close can cancel the upstream mux
        # handshake; the actual L2CAP must still be closed and owned nowhere.
        assert client.l2cap_channel is None or client.l2cap_channel.state == client.l2cap_channel.State.CLOSED
        fresh = await manager.ensure_control(key)
        assert fresh is not hf and fresh._slc_initialized and not fresh.stopped
        assert len(gateways) == 2
        assert sum(kind == "call" and data.get("state") == "蓝牙语音服务已断开，可重连" for kind, data in events) == 1


async def test_malformed_at_closes_only_owned_control_and_keeps_acl_recoverable(monkeypatch):
    async with managed_control(monkeypatch) as (manager, connection, key, hf, gateways, events):
        gateways[-1].dlc.write(b"\r\n+VGS: )\r\n")
        await until(lambda: key not in manager.protocols and key not in manager.clients and not manager.cleanup_tasks)
        assert hf.stopped and manager.connections[key] is connection
        assert not manager.links and manager.microphone is None
        fresh = await manager.ensure_control(key)
        assert fresh is not hf and fresh._slc_initialized


async def test_control_dlc_close_explicitly_disconnects_its_still_live_voice_link(monkeypatch):
    import bluehead.calls as calls

    monkeypatch.setattr(calls, "Microphone", SyntheticMicrophone)
    async with managed_control(monkeypatch) as (manager, connection, key, hf, gateways, events):
        manager.device.host.send_sco_sdu = lambda *args: None
        link = voice_link(hci.CodecID.CVSD)
        manager.on_link(key, hf, link)
        await manager.capture_tasks[key]
        codec, microphone = manager.voice_codecs[key], manager.microphone
        await hf.dlc.disconnect()
        await until(lambda: link.disconnected and key not in manager.links and microphone.closed)
        assert codec.handle is None and manager.router.owner is None
        assert manager.connections[key] is connection


async def test_stopped_slc_cannot_accept_a_delayed_sco_request_or_link(monkeypatch):
    async with managed_control(monkeypatch) as (manager, connection, key, hf, gateways, events):
        commands = []

        async def send(command):
            commands.append(command)

        monkeypatch.setattr(manager.device, "send_async_command", send)
        hf.stop()
        await manager.accept_audio(key, hf, connection, hci.HCI_Connection_Complete_Event.LinkType.ESCO)
        assert not any(
            isinstance(command, (hci.HCI_Accept_Synchronous_Connection_Request_Command,
                                 hci.HCI_Enhanced_Accept_Synchronous_Connection_Request_Command))
            for command in commands
        )
        link = voice_link(hci.CodecID.CVSD)
        manager.on_link(key, hf, link)
        await asyncio.sleep(0)
        assert link.disconnected and key not in manager.links


@pytest.mark.parametrize(
    "failure",
    [hci.HCI_UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR,
     hci.HCI_INVALID_HCI_COMMAND_PARAMETERS_ERROR,
     hci.HCI_UNSUPPORTED_REMOTE_FEATURE_ERROR],
)
async def test_codec_related_hci_failure_requests_real_peer_renegotiation(monkeypatch, failure):
    async with managed_control(monkeypatch) as (manager, connection, key, hf, gateways, events):
        ag = gateways[-1]
        await ag.negotiate_codec(hfp.AudioCodec.LC3_SWB)
        await until(lambda: hf.active_codec == hfp.AudioCodec.LC3_SWB)
        requests = []
        ag.on("codec_connection_request", lambda: requests.append(True))
        connection.emit("sco_connection_failure", failure)
        await until(lambda: bool(requests))
        assert ag.supported_audio_codecs == hf.supported_audio_codecs == [1, 2]
        assert hf.active_codec == ag.active_codec == hfp.AudioCodec.LC3_SWB
        await ag.negotiate_codec(hfp.AudioCodec.MSBC)
        await until(lambda: hf.active_codec == hfp.AudioCodec.MSBC)
        assert key not in manager.links


@pytest.mark.parametrize(
    "failure",
    [hci.HCI_CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES_ERROR,
     hci.HCI_CONNECTION_TIMEOUT_ERROR,
     hci.HCI_CONNECTION_TERMINATED_BY_LOCAL_HOST_ERROR],
)
async def test_resource_timeout_and_normal_cancellation_do_not_withdraw_codecs(monkeypatch, failure):
    async with managed_control(monkeypatch) as (manager, connection, key, hf, gateways, events):
        ag = gateways[-1]
        await ag.negotiate_codec(hfp.AudioCodec.LC3_SWB)
        await until(lambda: hf.active_codec == hfp.AudioCodec.LC3_SWB)
        requests = []
        ag.on("codec_connection_request", lambda: requests.append(True))
        connection.emit("sco_connection_failure", failure)
        await asyncio.sleep(0.01)
        assert not requests and hf.supported_audio_codecs == [1, 2, 3]
        assert hf.active_codec == hfp.AudioCodec.LC3_SWB


@pytest.mark.parametrize("replace", [False, True])
async def test_telemetry_racing_real_codec_close_cannot_escape_or_describe_a_new_link(monkeypatch, replace):
    manager = CallManager(Network(1).devices[0], Mixer(), lambda *args, **kwargs: None)
    entered, release, really_closed = threading.Event(), threading.Event(), threading.Event()
    codec = VoiceCodec(3)
    original_stats = codec.stats

    def blocked_stats():
        entered.set()
        assert release.wait(2)
        try:
            return original_stats()
        except RuntimeError:
            # This is the real wrapper's _require_open() after native destroy,
            # rather than a mocked exception pretending to exercise the race.
            really_closed.set()
            raise

    monkeypatch.setattr(codec, "stats", blocked_stats)
    manager.links["peer"] = voice_link()
    manager.voice_codecs["peer"] = codec
    manager.statistics["peer"] = {"codec": "lc3_swb", "rate": 32000}
    task = asyncio.create_task(asyncio.to_thread(manager.snapshot, "peer"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        manager.audio_closed("peer")
        assert codec.handle is None
        if replace:
            manager.links["peer"] = voice_link()
            manager.voice_codecs["peer"] = VoiceCodec(2)
            manager.statistics["peer"] = {"codec": "msbc", "rate": 16000}
        release.set()
        assert await asyncio.wait_for(task, 1) == {}
        assert really_closed.is_set()
        if replace:
            current = manager.snapshot("peer")
            assert current["active"] and current["codec"] == "msbc"
            assert current["rate"] == 16000 and current["voice"]["codec_id"] == 2
        else:
            assert manager.snapshot("peer") == {}
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        manager.close()
        await manager.wait_closed()
        codec.close()


async def test_optional_voice_telemetry_oserror_does_not_discard_live_link(monkeypatch):
    manager = CallManager(Network(1).devices[0], Mixer(), lambda *args, **kwargs: None)
    codec = VoiceCodec(3)

    def unavailable_stats():
        raise OSError("native telemetry unavailable")

    monkeypatch.setattr(codec, "stats", unavailable_stats)
    manager.links["peer"] = voice_link()
    manager.voice_codecs["peer"] = codec
    manager.statistics["peer"] = {"codec": "lc3_swb", "rate": 32000}
    try:
        result = manager.snapshot("peer")
        assert result["active"] and result["codec"] == "lc3_swb"
        assert result["voice"] == {} and codec.handle is not None
    finally:
        manager.close()
        await manager.wait_closed()
        codec.close()

"""Real in-memory BR/EDR, L2CAP and AVCTP signalling; no radio/audio devices."""
import asyncio
from contextlib import asynccontextmanager
import random
import struct
from types import SimpleNamespace

import pytest
from bumble import avc, avctp, avrcp, core
from bumble.controller import Controller
from bumble.device import Device
from bumble.host import Host
from bumble.link import LocalLink
from bumble.transport.common import AsyncPipeSink

from bluehead.avrcp import (AvrcpManager, AvrcpError, PeerDisconnected, Rejected,
                           CT_RECORD, TG_RECORD, MAX_PDU, _Transport, _metadata, _vendor)


async def until(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


class Network:
    def __init__(self, count):
        self.link, self.devices = LocalLink(), []
        for index in range(count):
            address = f"F0:F1:F2:F3:F4:{index + 1:02X}"
            controller = Controller(f"avrcp-{index}", link=self.link, public_address=address)
            device = Device(name=f"avrcp-{index}", address=address,
                            host=Host(controller, AsyncPipeSink(controller)))
            device.classic_enabled, device.le_enabled = True, False
            self.devices.append(device)

    async def start(self):
        for device in self.devices:
            await device.power_on()
            await device.set_connectable(True)

    async def close(self):
        for device in self.devices:
            await device.power_off()


class PhoneDelegate(avrcp.Delegate):
    def __init__(self):
        super().__init__(supported_events=[avrcp.EventId.PLAYBACK_STATUS_CHANGED, avrcp.EventId.TRACK_CHANGED])
        self.playback_status = avrcp.PlayStatus.PLAYING
        self.current_track_uid = 1
        self.keys = []

    async def on_key_event(self, key, pressed, data):
        self.keys.append((key, pressed))


class Phone(avrcp.Protocol):
    def __init__(self, title="蓝色音乐 🎵", fragment_size=None):
        super().__init__(PhoneDelegate())
        self.title, self.fragment_size = title, fragment_size
        self.remaining = b""
        self.continuations = 0
        self.drop = set()
        self.requests = []

    def attributes(self):
        values = [(1, self.title), (2, "艺术家"), (3, "Album"), (7, "123456")]
        return bytes([len(values)]) + b"".join(struct.pack(">IHH", key, 106, len(value.encode())) + value.encode()
                                               for key, value in values)

    def fragment(self, label, parameters, kind):
        frame = _vendor(avc.ResponseFrame.ResponseCode.IMPLEMENTED_OR_STABLE,
                        avrcp.PduId.GET_ELEMENT_ATTRIBUTES, parameters, kind)
        self.avctp_protocol.send_response(label, avrcp.AVRCP_PID, bytes(frame))

    def _on_command_pdu(self, pdu_id, pdu):
        label = self.receive_command_state.transaction_label
        self.requests.append(pdu_id)
        if pdu_id in self.drop:
            self.receive_command_state = None
            return
        if pdu_id == avrcp.PduId.GET_ELEMENT_ATTRIBUTES:
            parameters = self.attributes()
            if self.fragment_size and len(parameters) > self.fragment_size:
                self.remaining = parameters[self.fragment_size:]
                self.fragment(label, parameters[:self.fragment_size], 1)
            else:
                self.fragment(label, parameters, 0)
            self.receive_command_state = None
        elif pdu_id == avrcp.PduId.REQUEST_CONTINUING_RESPONSE:
            self.continuations += 1
            part, self.remaining = self.remaining[:self.fragment_size], self.remaining[self.fragment_size:]
            self.fragment(label, part, 2 if self.remaining else 3)
            self.receive_command_state = None
        elif pdu_id == avrcp.PduId.ABORT_CONTINUING_RESPONSE:
            self.remaining = b""
            self.send_avrcp_response(label, avc.ResponseFrame.ResponseCode.ACCEPTED,
                                    avrcp.NotImplementedResponse(pdu_id, b""))
            self.receive_command_state = None
        else:
            super()._on_command_pdu(pdu_id, pdu)


@asynccontextmanager
async def connected(*, title="蓝色音乐 🎵", fragment_size=None, count=2, timeout=.3):
    network = Network(count)
    events, volumes = [], []
    manager = AvrcpManager(lambda kind, **data: events.append(data), lambda *args: volumes.append(args),
                           command_timeout=timeout, auto_connect=False).attach(network.devices[0])
    phones, connections = [], []
    for device in network.devices[1:]:
        phone = Phone(title, fragment_size)
        phone.listen(device)
        phones.append(phone)
    await network.start()
    try:
        for device in network.devices[1:]:
            connection = await network.devices[0].connect(device.public_address, transport=core.PhysicalTransport.BR_EDR)
            connections.append(connection)
            await manager.connect(connection)
        await until(lambda: all(p.avctp_protocol is not None for p in phones))
        await until(lambda: all(p.state["title"] for p in manager.peers.values()))
        yield manager, phones, connections, events, volumes
    finally:
        await manager.aclose()
        await network.close()


async def test_real_sdp_metadata_notifications_and_press_release():
    async with connected() as (manager, phones, links, events, _):
        peer, phone = next(iter(manager.peers.values())), phones[0]
        assert manager.snapshot(peer.key)["title"] == "蓝色音乐 🎵"
        assert peer.state["artist"] == "艺术家" and peer.state["album"] == "Album"
        assert peer.state["duration_ms"] == 123456
        assert peer.state["playback_status"] == "playing"
        assert {CT_RECORD, TG_RECORD} <= manager.device.sdp_service_records.keys()
        phone_link = phone.avctp_protocol.l2cap_channel.connection
        wire_ct = await avrcp.ControllerServiceSdpRecord.find(phone_link)
        wire_tg = await avrcp.TargetServiceSdpRecord.find(phone_link)
        assert len(wire_ct) == len(wire_tg) == 1
        assert wire_ct[0].supported_features == avrcp.ControllerFeatures.CATEGORY_1
        assert wire_tg[0].supported_features == avrcp.TargetFeatures.CATEGORY_2
        for action in ("play_pause", "next", "previous", "stop"):
            await manager.command(peer.key, action)
        assert phone.delegate.keys == [(key, pressed) for key in
            (avc.PassThroughFrame.OperationId.PAUSE, avc.PassThroughFrame.OperationId.FORWARD,
             avc.PassThroughFrame.OperationId.BACKWARD, avc.PassThroughFrame.OperationId.STOP)
            for pressed in (True, False)]
        phone.delegate.playback_status = avrcp.PlayStatus.PAUSED
        phone.notify_playback_status_changed(avrcp.PlayStatus.PAUSED)
        await until(lambda: peer.state["playback_status"] == "paused")
        await until(lambda: avrcp.EventId.PLAYBACK_STATUS_CHANGED in phone.notification_listeners)
        await manager.command(peer.key, "play_pause")
        assert phone.delegate.keys[-2:] == [(avc.PassThroughFrame.OperationId.PLAY, True),
                                           (avc.PassThroughFrame.OperationId.PLAY, False)]
        phone.title = "下一首"
        phone.delegate.current_track_uid = 2
        phone.notify_track_changed(2)
        await until(lambda: peer.state["title"] == "下一首")
        await until(lambda: avrcp.EventId.TRACK_CHANGED in phone.notification_listeners)
        phone.delegate.current_track_uid = avrcp.TrackChangedEvent.NO_TRACK
        phone.notify_track_changed(avrcp.TrackChangedEvent.NO_TRACK)
        await until(lambda: peer.state["title"] == "")
        assert peer.state["duration_ms"] is None


async def test_long_unicode_continuation_over_sixteen_fragments_reuses_labels():
    title = "音符🎵" * 600
    async with connected(title=title, fragment_size=80) as (manager, phones, *_):
        peer = next(iter(manager.peers.values()))
        assert peer.state["title"] == title[:1024]
        assert phones[0].continuations > 16
        assert len(peer.pending) == 2  # Only two standing event subscriptions.
        assert len(peer.labels) == 14


async def test_volume_target_no_feedback_and_subscription_reregistration():
    async with connected() as (manager, phones, links, events, volumes):
        phone, peer = phones[0], next(iter(manager.peers.values()))
        supported = await phone.get_supported_events()
        assert supported == [avrcp.EventId.VOLUME_CHANGED]
        initial = await phone.send_avrcp_command(avc.CommandFrame.CommandType.NOTIFY,
            avrcp.RegisterNotificationCommand(avrcp.EventId.VOLUME_CHANGED, 0))
        assert isinstance(initial, avrcp.Protocol.InterimResponse)
        assert initial.response.event.volume == 127
        result = await phone.send_avrcp_command(avc.CommandFrame.CommandType.CONTROL, avrcp.SetAbsoluteVolumeCommand(32))
        assert result.response.volume == 32
        assert volumes[-1] == (peer.key, 32 / 127)
        assert not initial.final.done()  # SetAbsoluteVolume must not complete notification.
        manager.set_volume(peer.key, .5)
        changed = await asyncio.wait_for(initial.final, .5)
        assert changed.response.event.volume == 64
        assert peer.volume_listener is None
        assert volumes[-1] == (peer.key, 32 / 127)  # local UI causes no callback loop
        # The next local change is silent until a new subscription is registered.
        manager.set_volume(peer.key, .2)
        assert peer.volume == 25
        response = await phone.send_avrcp_command(avc.CommandFrame.CommandType.CONTROL, avrcp.SetAbsoluteVolumeCommand(255))
        assert isinstance(response.response, avrcp.RejectedResponse)
        assert peer.volume == 25


async def test_relative_volume_keys_and_unit_subunit_info_on_wire():
    async with connected() as (manager, phones, *_):
        phone, peer = phones[0], next(iter(manager.peers.values()))
        await phone.send_key_event(avc.PassThroughFrame.OperationId.VOLUME_DOWN, True)
        await phone.send_key_event(avc.PassThroughFrame.OperationId.VOLUME_DOWN, False)
        assert peer.volume == 119
        responses = []
        phone.avctp_protocol.register_response_handler(avrcp.AVRCP_PID,
            lambda label, data: responses.append((label, avc.Frame.from_bytes(data))))
        for opcode in (avc.Frame.OperationCode.UNIT_INFO, avc.Frame.OperationCode.SUBUNIT_INFO):
            command = avc.CommandFrame(avc.CommandFrame.CommandType.STATUS, avc.Frame.SubunitType.UNIT, 7,
                                       opcode, b"\x07\xff\xff\xff\xff")
            phone.avctp_protocol.send_command(15, avrcp.AVRCP_PID, bytes(command))
            await until(lambda: len(responses) == (1 if opcode == avc.Frame.OperationCode.UNIT_INFO else 2))
        assert all(frame.response == avc.ResponseFrame.ResponseCode.IMPLEMENTED_OR_STABLE for _, frame in responses)
        assert responses[-1][1].operands == b"\x07\x48\xff\xff\xff"


async def test_two_acl_sessions_do_not_share_notifications_or_volume():
    async with connected(count=3) as (manager, phones, links, events, volumes):
        first, second = list(manager.peers.values())
        phones[0].delegate.playback_status = avrcp.PlayStatus.PAUSED
        phones[0].notify_playback_status_changed(avrcp.PlayStatus.PAUSED)
        await until(lambda: first.state["playback_status"] == "paused")
        assert second.state["playback_status"] == "playing"
        await phones[1].send_avrcp_command(avc.CommandFrame.CommandType.CONTROL, avrcp.SetAbsoluteVolumeCommand(8))
        assert first.volume == 127 and second.volume == 8
        await manager.command(second.key, "next")
        assert not phones[0].delegate.keys and phones[1].delegate.keys
        await manager.disconnect(first.key)
        assert not first.state["connected"] and second.state["connected"]
        await manager.connect(links[0])
        await until(lambda: first.state["title"] != "")


async def test_timeout_cancellation_and_disconnect_return_transaction_slots():
    async with connected(timeout=.06) as (manager, phones, links, *_):
        peer, phone = next(iter(manager.peers.values())), phones[0]
        await until(lambda: peer.metadata_task.done())
        baseline = len(peer.pending)
        phone.drop.add(avrcp.PduId.GET_PLAY_STATUS)
        with pytest.raises(TimeoutError):
            await peer.refresh_status()
        assert len(peer.pending) == baseline
        assert len(peer.quarantined) == 1
        task = asyncio.create_task(peer.refresh_status())
        await until(lambda: len(peer.pending) > baseline)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(peer.pending) == baseline
        outstanding = asyncio.create_task(peer.refresh_status())
        await until(lambda: len(peer.pending) > baseline)
        await manager.disconnect(peer.key)
        with pytest.raises(asyncio.CancelledError):
            await outstanding
        assert not peer.pending and len(peer.labels) == 16
        assert not peer.quarantined
        # A disconnected channel cannot complete a new connection's transaction.
        assert manager.snapshot(peer.key)["connected"] is False
        with pytest.raises(PeerDisconnected):
            await manager.command(peer.key, "play")


@pytest.mark.parametrize("data", [b"", b"\x01", b"\x01" + struct.pack(">IHH", 1, 106, 10) + b"short",
                                  b"\x00trailing", bytes(MAX_PDU + 1)],
                         ids=["empty", "missing_attribute", "overrun", "trailing", "oversize"])
def test_metadata_rejects_lengths(data):
    with pytest.raises(ValueError):
        _metadata(data)


def test_metadata_character_sets_invalid_unicode_and_unknown_fields():
    def attribute(key, charset, data):
        return struct.pack(">IHH", key, charset, len(data)) + data
    result = _metadata(b"\x04" + attribute(1, 1013, "音乐".encode("utf-16-be")) + attribute(2, 106, b"bad\xff") +
                       attribute(3, 999, b"unknown") + attribute(7, 106, b"99999999999999999999"))
    assert result == dict(title="音乐", artist="bad�", album="", duration_ms=None)


def test_avctp_fragmentation_wire_layout_bounds_and_interleaving():
    emitted, received = [], []
    channel = SimpleNamespace(peer_mtu=48, write=emitted.append)
    transport = _Transport(channel, lambda *args: received.append(args), .1)
    payload = bytes(range(256)) * 3
    transport.send(4, False, payload)
    assert all(len(packet) <= 48 for packet in emitted)
    assert emitted[0][:4] == b"\x46\x11\x11\x0e"  # START, seventeen packets, PID.
    assert emitted[1][0] == 0x4A and emitted[-1][0] == 0x4E
    for packet in emitted:
        transport.receive(packet)
    assert received == [(4, False, False, avrcp.AVRCP_PID, payload)]
    received.clear()
    for bad in (b"", b"\x04", b"\x04\x01\x11\x0e", b"\x08", b"\x00\x11"):
        transport.receive(bad)
    assert not received
    transport.receive(emitted[0])
    transport.receive(emitted[1][:-1])  # Non-final fragments must match start's SDU length.
    assert not transport.fragments


async def test_attach_idempotent_and_shutdown_removes_only_own_sdp():
    network = Network(1)
    device = network.devices[0]
    device.sdp_service_records[0x10001] = []
    manager = AvrcpManager(auto_connect=False)
    manager.attach(device).attach(device)
    assert len(device.listeners("connection")) == 1
    await manager.aclose()
    await manager.aclose()
    assert device.sdp_service_records == {0x10001: []}
    assert avctp.AVCTP_PSM not in device.l2cap_channel_manager.servers


async def test_incoming_connection_and_acl_reconnect_have_fresh_peer_lifetime():
    network = Network(2)
    manager = AvrcpManager(auto_connect=False).attach(network.devices[0])
    phone = Phone()
    await network.start()
    try:
        async def incoming():
            link = await network.devices[1].connect(network.devices[0].public_address,
                                                   transport=core.PhysicalTransport.BR_EDR)
            await phone.connect(link)
            await until(lambda: any(p.state["title"] for p in manager.peers.values()))
            return link, next(iter(manager.peers.values()))
        link, first = await incoming()
        await link.disconnect()
        await until(lambda: not manager.peers)
        assert first.closed and not first.pending
        link, second = await incoming()
        assert first is not second and second.state["title"] == phone.title
    finally:
        await manager.aclose()
        await network.close()


async def test_real_simultaneous_control_connections_recover_to_one_channel():
    network = Network(2)
    left = AvrcpManager(auto_connect=False).attach(network.devices[0])
    right = AvrcpManager(auto_connect=False).attach(network.devices[1])
    await network.start()
    try:
        first = await network.devices[0].connect(network.devices[1].public_address,
                                                transport=core.PhysicalTransport.BR_EDR)
        await until(lambda: bool(right.peers))
        second = next(iter(right.peers.values())).connection
        await asyncio.gather(left.connect(first), right.connect(second))
        await until(lambda: all(next(iter(manager.peers.values())).state["connected"]
                                for manager in (left, right)), timeout=4)
        # Allow both collision-close callbacks and retry attempts to finish.
        await asyncio.sleep(.12)
        assert all(next(iter(manager.peers.values())).channel is not None for manager in (left, right))
    finally:
        await left.aclose()
        await right.aclose()
        await network.close()


async def test_late_response_to_abandoned_label_cannot_complete_next_request():
    async with connected(timeout=.06) as (manager, phones, *_):
        peer, phone = next(iter(manager.peers.values())), phones[0]
        phone.drop.add(avrcp.PduId.GET_PLAY_STATUS)
        with pytest.raises(TimeoutError):
            await peer.refresh_status()
        old_label = next(iter(peer.quarantined))
        task = asyncio.create_task(peer.refresh_status())
        await until(lambda: any(t.pdu == avrcp.PduId.GET_PLAY_STATUS for t in peer.pending.values()))
        frame = _vendor(avc.ResponseFrame.ResponseCode.IMPLEMENTED_OR_STABLE,
                        avrcp.PduId.GET_PLAY_STATUS, struct.pack(">IIB", 300, 20, 1))
        phone.avctp_protocol.send_response(old_label, avrcp.AVRCP_PID, bytes(frame))
        await asyncio.sleep(.01)
        assert not task.done()
        with pytest.raises(TimeoutError):
            await task


async def test_key_rejection_still_sends_release_and_unknown_toggle_does_not_guess():
    async with connected() as (manager, phones, *_):
        peer, phone = next(iter(manager.peers.values())), phones[0]
        async def reject(key, pressed, data):
            phone.delegate.keys.append((key, pressed))
            raise avrcp.Delegate.AvcError(avc.ResponseFrame.ResponseCode.NOT_IMPLEMENTED)
        phone.delegate.on_key_event = reject
        with pytest.raises(Rejected):
            await manager.command(peer.key, "next")
        assert phone.delegate.keys[-2:] == [(avc.PassThroughFrame.OperationId.FORWARD, True),
                                           (avc.PassThroughFrame.OperationId.FORWARD, False)]
        peer.state["playback_status"] = "unknown"
        phone.delegate.playback_status = avrcp.PlayStatus.ERROR
        with pytest.raises(AvrcpError):
            await manager.command(peer.key, "play_pause")
        assert len(phone.delegate.keys) == 2


async def test_unsupported_notification_finishes_without_leaking_transaction():
    async with connected() as (manager, phones, *_):
        peer = next(iter(manager.peers.values()))
        baseline = len(peer.pending)
        await peer.monitor(avrcp.EventId.PLAYBACK_POS_CHANGED)
        assert len(peer.pending) == baseline
        assert not peer.quarantined


async def test_bad_notification_response_ends_subscription_safely():
    async with connected() as (manager, phones, *_):
        peer, phone = next(iter(manager.peers.values())), phones[0]
        label = phone.notification_listeners[avrcp.EventId.PLAYBACK_STATUS_CHANGED].transaction_label
        malformed = _vendor(avc.ResponseFrame.ResponseCode.CHANGED, avrcp.PduId.REGISTER_NOTIFICATION, b"\x01")
        phone.avctp_protocol.send_response(label, avrcp.AVRCP_PID, bytes(malformed))
        await until(lambda: label not in peer.pending)
        assert peer.state["connected"]  # A malformed optional control never closes audio's ACL.
        assert label in peer.quarantined


async def test_metadata_no_track_notification_invalidates_inflight_refresh():
    async with connected(timeout=.3) as (manager, phones, *_):
        peer, phone = next(iter(manager.peers.values())), phones[0]
        await until(lambda: peer.metadata_task.done())
        phone.drop.add(avrcp.PduId.GET_ELEMENT_ATTRIBUTES)
        peer.refresh_metadata()
        await until(lambda: any(t.pdu == avrcp.PduId.GET_ELEMENT_ATTRIBUTES for t in peer.pending.values()))
        label = next(label for label, t in peer.pending.items() if t.pdu == avrcp.PduId.GET_ELEMENT_ATTRIBUTES)
        peer.handle_event(b"\x02" + b"\xff" * 8)
        phone.fragment(label, phone.attributes(), 0)
        await peer.metadata_task
        assert peer.state["title"] == "" and peer.state["duration_ms"] is None


async def test_attach_collision_keeps_unrelated_sdp_untouched():
    network = Network(1)
    device = network.devices[0]
    unrelated = ["other-service"]
    device.sdp_service_records[CT_RECORD] = unrelated
    manager = AvrcpManager()
    with pytest.raises(RuntimeError):
        manager.attach(device)
    await manager.aclose()
    assert device.sdp_service_records[CT_RECORD] is unrelated


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")], ids=["nan", "inf", "negative_inf"])
async def test_nonfinite_ui_volume_rejected_without_audio_callback(value):
    async with connected() as (manager, phones, _, events, volumes):
        peer = next(iter(manager.peers.values()))
        with pytest.raises(ValueError):
            manager.set_volume(peer.key, value)
        assert not volumes and peer.volume == 127


async def test_exhausted_abandoned_labels_rebuild_only_control_channel():
    async with connected() as (manager, phones, links, *_):
        peer = next(iter(manager.peers.values()))
        await until(lambda: peer.metadata_task.done())
        old_channel = peer.channel
        while peer.labels:
            peer.quarantined[peer.labels.popleft()] = True
        with pytest.raises(AvrcpError):
            await peer.refresh_status()
        assert peer.connection is links[0]  # The same ACL survives control recovery.
        await until(lambda: peer.channel is not None and peer.channel is not old_channel)
        await until(lambda: bool(peer.state["title"]))
        assert not peer.quarantined and len(peer.pending) == 2


async def test_fragment_limit_aborts_metadata_and_releases_slot():
    async with connected(fragment_size=80) as (manager, phones, *_):
        peer, phone = next(iter(manager.peers.values())), phones[0]
        await until(lambda: peer.metadata_task.done())
        phone.title = "a" * 22000
        phone.fragment_size = 10
        peer.refresh_metadata()
        await until(lambda: peer.metadata_task.done())
        await until(lambda: not phone.remaining)
        assert peer.state["title"] == "蓝色音乐 🎵"
        assert len(peer.pending) == 2
        assert len(peer.quarantined) == 1


def test_avctp_random_malformed_frames_stay_bounded():
    received = []
    channel = SimpleNamespace(peer_mtu=672, write=lambda _: None)
    transport = _Transport(channel, lambda *args: received.append(args), .01)
    generator = random.Random(17)
    for _ in range(3000):
        transport.receive(generator.randbytes(generator.randrange(0, 80)))
    assert len(transport.fragments) <= 32
    assert all(len(state[3]) <= MAX_PDU + 10 for state in transport.fragments.values())


async def test_invalid_tg_packets_do_not_break_later_media_controls():
    async with connected() as (manager, phones, *_):
        peer, phone = next(iter(manager.peers.values())), phones[0]
        for malformed in (b"", b"\x00", b"\x00\x48", b"\x00\x48\x00\x00\x19\x58\x50\x00\x00\x05\x7f"):
            phone.avctp_protocol.send_command(15, avrcp.AVRCP_PID, malformed)
        await manager.command(peer.key, "next")
        assert phone.delegate.keys[-1] == (avc.PassThroughFrame.OperationId.FORWARD, False)
        assert peer.state["connected"]

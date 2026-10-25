"""Receiver/engine integration over virtual Bluetooth, with native offline PCM."""
import asyncio
from contextlib import asynccontextmanager
from io import StringIO
import json
import logging
from types import SimpleNamespace

import numpy as np
import pytest
from bumble import a2dp, avc, avdtp, avrcp, core, sdp

from bluehead.audio import Mixer
from bluehead.backend import Receiver, Settings
from bluehead.engine import Engine
from bluehead.protocol import capabilities
from .test_avrcp import Network, Phone, until
from .vectors import encode, signal, wire_packets


@asynccontextmanager
async def receiver_peers(tmp_path, count=2):
    network = Network(count)
    engine = Engine(tmp_path, StringIO())
    engine.settings = Settings(calls=False)
    receiver = Receiver(engine.settings, tmp_path, engine.emit, Mixer(buffer_ms=0, max_ms=1000))
    receiver.attach(network.devices[0])
    receiver.avrcp.auto_connect = False
    actions = []
    engine.runner = SimpleNamespace(receiver=receiver, thread=SimpleNamespace(is_alive=lambda: True),
        action=lambda operation, key: actions.append(asyncio.create_task(receiver.action(operation, key))))
    phones = []
    for device in network.devices[1:]:
        phone = Phone()
        phone.listen(device)
        phones.append(phone)
    await network.start()
    links = []
    try:
        for device in network.devices[1:]:
            link = await device.connect(network.devices[0].public_address, transport=core.PhysicalTransport.BR_EDR)
            links.append(link)
        await until(lambda: len(receiver.connections) == count - 1)
        for connection in list(receiver.connections.values()):
            await receiver.avrcp.connect(connection)
        await until(lambda: all(peer.state["title"] for peer in receiver.avrcp.peers.values()))
        yield receiver, engine, phones, links, actions
    finally:
        receiver.stop_event.set()
        for session in list(receiver.sessions.values()):
            session.close()
        if receiver.media_watch_task:
            receiver.media_watch_task.cancel()
            await asyncio.gather(receiver.media_watch_task, return_exceptions=True)
        await receiver.avrcp.aclose()
        await asyncio.gather(*actions, return_exceptions=True)
        await network.close()


async def test_receiver_absolute_volume_preserves_independent_200_percent_gain(tmp_path):
    async with receiver_peers(tmp_path, 3) as (receiver, engine, phones, links, _):
        first, second = list(receiver.avrcp.peers.values())
        for peer in (first, second):
            receiver.mixer.add(peer.key)
            receiver.mixer.add("call:" + peer.key)
        engine.command(dict(protocol=1, command="gain", key=first.key, direction="down", gain=1.75))
        subscribed = await phones[0].send_avrcp_command(avc.CommandFrame.CommandType.NOTIFY,
            avrcp.RegisterNotificationCommand(avrcp.EventId.VOLUME_CHANGED, 0))
        await phones[0].send_avrcp_command(avc.CommandFrame.CommandType.CONTROL, avrcp.SetAbsoluteVolumeCommand(32))
        assert receiver.mixer.inputs[first.key].gain == pytest.approx(1.75 * 32 / 127)
        assert receiver.mixer.inputs["call:" + first.key].gain == 1.75
        assert receiver.mixer.inputs[second.key].gain == 1
        assert engine.settings.device_volumes[first.key]["down"] == 1.75
        assert not subscribed.final.done()
        engine.command(dict(protocol=1, command="gain", key=first.key, direction="down", gain=2))
        assert receiver.mixer.inputs[first.key].gain == pytest.approx(2 * 32 / 127)
        assert first.volume == 32 and not subscribed.final.done()
        saved = json.loads((tmp_path / "settings.json").read_text("utf-8"))
        assert saved["device_volumes"][first.key]["down"] == 2
        assert "standard_volumes" not in saved


async def test_engine_media_action_whitelist_reaches_receiver_without_metadata_logging(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="bluehead.engine")
    async with receiver_peers(tmp_path) as (receiver, engine, phones, _, actions):
        peer = next(iter(receiver.avrcp.peers.values()))
        for operation in ("play", "pause", "stop", "next", "previous"):
            engine.command(dict(protocol=1, command="device_action", key=peer.key, action=operation))
            await actions[-1]
        assert len(phones[0].delegate.keys) == 10
        with pytest.raises(ValueError):
            engine.command(dict(protocol=1, command="device_action", key=peer.key, action="eval"))
        private = "Private listening title 🎵"
        engine.emit("avrcp", key=peer.key, title=private, artist="Private artist")
        assert private in engine.output.getvalue()
        assert private not in caplog.text and "Private artist" not in caplog.text
        assert not (tmp_path / "settings.json").exists()


@pytest.mark.parametrize("codec", ["sbc", "aac"])
async def test_apple_style_44100_media_and_controls_survive_suspend_resume(tmp_path, codec):
    async with receiver_peers(tmp_path) as (receiver, engine, phones, links, actions):
        link, phone = links[0], phones[0]
        protocol = await avdtp.Protocol.connect(link)
        endpoints = list(await protocol.discover_remote_endpoints())
        selected = capabilities(codec, selected=True)
        if codec == "aac":
            info = a2dp.AacMediaCodecInformation
            selected.media_codec_information = info(info.ObjectType.MPEG_2_AAC_LC,
                info.SamplingFrequency.SF_44100, info.Channels.STEREO, 1, 264630)
        else:
            info = a2dp.SbcMediaCodecInformation
            selected.media_codec_information = info(info.SamplingFrequency.SF_44100,
                info.ChannelMode.STEREO, info.BlockLength.BL_16, info.Subbands.S_8,
                info.AllocationMethod.LOUDNESS, 2, 53)
        # Bumble serializes capability bytes at construction, not on mutation.
        selected = avdtp.MediaCodecCapabilities(selected.media_type, selected.media_codec_type,
                                                selected.media_codec_information)
        remote = next(endpoint for endpoint in endpoints if any(
            isinstance(c, avdtp.MediaCodecCapabilities) and c.media_codec_type == selected.media_codec_type
            for c in endpoint.capabilities))
        local = protocol.add_source(selected, None)
        stream = await protocol.create_stream(local, remote)
        await stream.start()
        peer = next(iter(receiver.avrcp.peers.values()))
        packet_data = list(wire_packets(codec, encode(codec, signal(44100, .2), 44100), 44100))
        for turn in range(2):
            for packet in packet_data:
                stream.rtp_channel.write(packet)
                await asyncio.sleep(.003)
            await until(lambda: receiver.mixer.inputs[peer.key].frames > 4000)
            assert np.std(receiver.mixer.render(4000)) > .005
            engine.command(dict(protocol=1, command="device_action", key=peer.key, action="next"))
            await actions[-1]
            assert phone.delegate.keys[-1] == (avc.PassThroughFrame.OperationId.FORWARD, False)
            assert receiver.sessions[peer.key].errors == 0
            if turn == 0:
                await stream.stop()
                assert receiver.mixer.inputs[peer.key].frames == 0
                await stream.start()
        assert receiver.avrcp.snapshot(peer.key)["connected"]
        assert receiver.sessions[peer.key].format.rate == 44100
        await stream.close()


async def test_receiver_sdp_server_and_audio_roles_are_queryable_together(tmp_path):
    async with receiver_peers(tmp_path) as (receiver, engine, phones, links, _):
        link = links[0]
        async with sdp.Client(link) as client:
            records = await client.search_attributes([core.UUID.from_16_bits(0x1000)], [0x0200, 0x0201])
        assert len(records) == 1
        assert sdp.ServiceAttribute.find_attribute_in_list(records[0], 0x0200).value
        assert sdp.ServiceAttribute.find_attribute_in_list(records[0], 0x0201).value >= 0
        ct = await avrcp.ControllerServiceSdpRecord.find(link)
        tg = await avrcp.TargetServiceSdpRecord.find(link)
        assert ct[0].supported_features == avrcp.ControllerFeatures.CATEGORY_1
        assert tg[0].supported_features == avrcp.TargetFeatures.CATEGORY_2
        data = core.AdvertisingData.from_bytes(receiver.device.inquiry_response)
        roles = {item.to_hex_str() for item in data.get(core.AdvertisingData.COMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS)}
        assert roles == {"110B", "110C", "110E", "110F"}
        assert data.get(core.AdvertisingData.DEVICE_ID) is None


async def test_receiver_repeated_acl_reconnect_cleans_only_disconnected_peer(tmp_path):
    async with receiver_peers(tmp_path, 3) as (receiver, engine, phones, links, _):
        first, other = list(receiver.avrcp.peers.values())
        key, source = first.key, links[0].device
        link = links[0]
        for _ in range(3):
            old = receiver.avrcp.peers[key]
            await link.disconnect()
            await until(lambda: key not in receiver.avrcp.peers and key not in receiver.connections)
            await until(lambda: not old.tasks)
            assert old.closed and not old.pending and len(old.labels) == 16
            assert receiver.avrcp.peers[other.key] is other and other.state["connected"]
            link = await source.connect(receiver.device.public_address, transport=core.PhysicalTransport.BR_EDR)
            await until(lambda: key in receiver.connections)
            await receiver.avrcp.connect(receiver.connections[key])
            await until(lambda: bool(receiver.avrcp.snapshot(key)["title"]))
            assert receiver.avrcp.peers[key] is not old
            assert len(receiver.avrcp.peers) == len(receiver.connections) == 2

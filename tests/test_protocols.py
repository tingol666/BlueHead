import asyncio
import random

import numpy as np
import pytest
from bumble import avdtp, core, hfp, rfcomm
from bumble.controller import Controller
from bumble.device import Device
from bumble.host import Host
from bumble.link import LocalLink
from bumble.transport.common import AsyncPipeSink

from bluehead.audio import Mixer
from bluehead.backend import Receiver, Settings
from bluehead.calls import CallManager
from bluehead.protocol import capabilities
from .vectors import signal, encode, wire_packets, LdacEncoder, rtp


class Network:
    def __init__(self, count=3):
        self.link = LocalLink()
        self.devices = []
        for index in range(count):
            address = f"F0:F1:F2:F3:F4:{index + 1:02X}"
            controller = Controller(f"test-{index}", link=self.link, public_address=address)
            device = Device(name=f"test-{index}", address=address, host=Host(controller, AsyncPipeSink(controller)))
            device.classic_enabled = True
            device.le_enabled = False
            self.devices.append(device)

    async def start(self):
        for device in self.devices:
            await device.power_on()
            await device.set_connectable(True)

    async def close(self):
        for device in self.devices:
            await device.power_off()


async def open_media(source_device, sink_device, codec):
    connection = await source_device.connect(sink_device.public_address, transport=core.PhysicalTransport.BR_EDR)
    protocol = await avdtp.Protocol.connect(connection)
    endpoints = list(await protocol.discover_remote_endpoints())
    offered = capabilities(codec)
    def matches(endpoint):
        for c in endpoint.capabilities:
            if isinstance(c, avdtp.MediaCodecCapabilities):
                if c.media_codec_type != offered.media_codec_type:
                    continue
                if codec in ("aptx", "aptx_hd", "ldac"):
                    return bytes(c.media_codec_information)[:6] == bytes(offered.media_codec_information)[:6]
                return True
        return False
    remote = next(e for e in endpoints if matches(e))
    local = protocol.add_source(capabilities(codec, selected=True), None)
    stream = await protocol.create_stream(local, remote)
    await stream.start()
    return connection, stream


@pytest.mark.asyncio
@pytest.mark.parametrize("codec", ["sbc", "aac", "aac_192", "aptx", "aptx_hd", "ldac"])
async def test_a2dp_real_signalling_and_decode(codec, tmp_path):
    network = Network(2)
    events = []
    mixer = Mixer(buffer_ms=0, max_ms=1000)
    receiver = Receiver(Settings(calls=False, ldac_enabled=True, codec=codec), tmp_path, lambda kind, **data: events.append((kind, data)), mixer)
    receiver.attach(network.devices[0])
    await network.start()
    try:
        async with asyncio.timeout(15):
            connection, stream = await open_media(network.devices[1], network.devices[0], codec)
            wire_codec = "aac" if codec == "aac_192" else codec
            reference = signal(48000, 0.2)
            if codec == "ldac":
                encoder = LdacEncoder()
                try:
                    packets = [rtp(p, i) for i, p in enumerate(encoder.encode(reference))]
                finally:
                    encoder.close()
            else:
                packets = list(wire_packets(wire_codec, encode(wire_codec, reference)))
                if codec in ("aptx", "aptx_hd"):
                    raw = encode(codec, reference)
                    grouped = [b"".join(raw[i:i + 120]) for i in range(0, len(raw), 120)]
                    packets = grouped if codec == "aptx" else [rtp(p, i) for i, p in enumerate(grouped)]
            for packet in packets:
                stream.rtp_channel.write(packet)
                await asyncio.sleep(0.003)
            for _ in range(100):
                if next(iter(mixer.inputs.values())).frames > 4000:
                    break
                await asyncio.sleep(0.01)
            assert events and any(e[0] == "format" and e[1]["codec"] == wire_codec for e in events)
            assert next(iter(mixer.inputs.values())).frames > 4000
            samples = mixer.render(4000)
            assert np.isfinite(samples).all() and np.std(samples) > 0.005
            assert next(iter(receiver.sessions.values())).errors == 0
            # Calls suspend A2DP, then the phone may restart RTP sequence and
            # codec state. Replay a fresh encoder run over the same connection.
            await stream.stop()
            assert next(iter(mixer.inputs.values())).frames == 0
            await stream.start()
            for packet in packets:
                stream.rtp_channel.write(packet)
                await asyncio.sleep(0.003)
            for _ in range(100):
                if next(iter(mixer.inputs.values())).frames > 4000:
                    break
                await asyncio.sleep(0.01)
            assert next(iter(mixer.inputs.values())).frames > 4000
            assert np.std(mixer.render(4000)) > 0.005
            assert next(iter(receiver.sessions.values())).errors == 0
            await stream.close()
            assert next(iter(mixer.inputs.values())).frames == 0
            await connection.disconnect()
            await asyncio.sleep(0.02)
            assert not receiver.sessions
    finally:
        for session in receiver.sessions.values():
            session.close()
        await network.close()


@pytest.mark.asyncio
async def test_two_sources_stream_at_the_same_time(tmp_path):
    network = Network(3)
    mixer = Mixer(buffer_ms=0, max_ms=1000)
    receiver = Receiver(Settings(calls=False), tmp_path, lambda *args, **kwargs: None, mixer)
    receiver.attach(network.devices[0])
    await network.start()
    try:
        async with asyncio.timeout(10):
            connection_a, a = await open_media(network.devices[1], network.devices[0], "aac")
            connection_b, b = await open_media(network.devices[2], network.devices[0], "sbc")
            pcm = signal(48000, 0.2)
            packets_a = list(wire_packets("aac", encode("aac", pcm)))
            packets_b = list(wire_packets("sbc", encode("sbc", pcm)))
            for i in range(max(len(packets_a), len(packets_b))):
                if i < len(packets_a):
                    a.rtp_channel.write(packets_a[i])
                if i < len(packets_b):
                    b.rtp_channel.write(packets_b[i])
                await asyncio.sleep(0.002)
            for _ in range(100):
                if all(stream.frames > 5000 for stream in mixer.inputs.values()):
                    break
                await asyncio.sleep(0.01)
            assert len(receiver.connections) == len(mixer.inputs) == 2
            assert all(stream.frames > 5000 for stream in mixer.inputs.values())
            assert all(s.errors == 0 for s in receiver.sessions.values())
            assert np.std(mixer.render(4000)) > 0.01
            await a.close()
            await b.close()
            await connection_a.disconnect()
            await connection_b.disconnect()
    finally:
        for session in list(receiver.sessions.values()):
            session.close()
        await network.close()


@pytest.mark.asyncio
async def test_hfp_slc_msbc_answer_and_hangup():
    network = Network(2)
    events = []
    manager = CallManager(network.devices[0], Mixer(), lambda kind, **data: events.append((kind, data)))
    await network.start()
    try:
        async with asyncio.timeout(10):
            connection = await network.devices[1].connect(network.devices[0].public_address, transport=core.PhysicalTransport.BR_EDR)
            mux = await rfcomm.Client(connection).start()
            channel = next(iter(manager.server.acceptors))
            dlc = await mux.open_dlc(channel)
            ag = hfp.AgProtocol(dlc, hfp.AgConfiguration(
                supported_ag_features=[hfp.AgFeature.CODEC_NEGOTIATION, hfp.AgFeature.REJECT_CALL, hfp.AgFeature.ESCO_S4_SETTINGS_SUPPORTED],
                supported_ag_indicators=[hfp.AgIndicatorState.call(), hfp.AgIndicatorState.callsetup(), hfp.AgIndicatorState.service()],
                supported_hf_indicators=[], supported_ag_call_hold_operations=[],
                supported_audio_codecs=[hfp.AudioCodec.CVSD, hfp.AudioCodec.MSBC],
            ))
            for _ in range(100):
                if manager.protocols and next(iter(manager.protocols.values()))._slc_initialized:
                    break
                await asyncio.sleep(0.01)
            key, hf = next(iter(manager.protocols.items()))
            assert hf._slc_initialized
            negotiated_codec = asyncio.Event()
            hf.on("codec_negotiation", lambda _: negotiated_codec.set())
            await ag.negotiate_codec(hfp.AudioCodec.MSBC)
            await negotiated_codec.wait()
            assert hf.active_codec == hfp.AudioCodec.MSBC
            answered = asyncio.Event()
            hungup = asyncio.Event()
            ag.on("answer", answered.set)
            ag.on("hang_up", hungup.set)
            await manager.action("answer", key)
            await answered.wait()
            await manager.action("hangup", key)
            await hungup.wait()
            assert manager.microphone is None  # no SCO link: physical mic must remain closed
            await connection.disconnect()
    finally:
        manager.close()
        await network.close()


@pytest.mark.asyncio
async def test_headset_establishes_hfp_when_phone_only_opens_acl():
    network = Network(2)
    events = []
    manager = CallManager(network.devices[0], Mixer(), lambda kind, **data: events.append((kind, data)))
    configuration = hfp.AgConfiguration(
        supported_ag_features=[hfp.AgFeature.CODEC_NEGOTIATION],
        supported_ag_indicators=[hfp.AgIndicatorState.call(), hfp.AgIndicatorState.callsetup(), hfp.AgIndicatorState.service()],
        supported_hf_indicators=[], supported_ag_call_hold_operations=[],
        supported_audio_codecs=[hfp.AudioCodec.CVSD, hfp.AudioCodec.MSBC],
    )
    gateways = []
    server = rfcomm.Server(network.devices[1])
    channel = server.listen(lambda dlc: gateways.append(hfp.AgProtocol(dlc, configuration)))
    network.devices[1].sdp_service_records = {0x10001: hfp.make_ag_sdp_records(0x10001, channel, configuration)}
    await network.start()
    try:
        async with asyncio.timeout(10):
            # A phone can leave HFP initiation to the headset after ACL connects.
            connection = await network.devices[1].connect(network.devices[0].public_address,
                                                           transport=core.PhysicalTransport.BR_EDR)
            while not manager.protocols or not next(iter(manager.protocols.values()))._slc_initialized:
                await asyncio.sleep(.02)
            assert gateways and any(kind == "call" and "已就绪" in data["state"] for kind, data in events)
            assert manager.microphone is None
            await connection.disconnect()
    finally:
        manager.close()
        await network.close()


def test_ldac_rejects_damaged_input_and_recovers():
    from bluehead.ldac import NativeLdac
    rng = random.Random(43)
    rejected = 0
    decoder = NativeLdac()
    try:
        for _ in range(12000):
            length = rng.randrange(4, 516)
            data = bytearray(rng.randbytes(length))
            # Plausible stereo header, random payload; exercise all parser paths.
            header = (0xAA << 16) | (rng.randrange(4) << 13) | (2 << 11) | ((length - 4) << 2)
            data[:3] = header.to_bytes(3, "big")
            try:
                out = decoder.decode(bytes(data))
                assert np.isfinite(out).all()
            except ValueError:
                rejected += 1
        assert rejected > 11000
    finally:
        decoder.close()

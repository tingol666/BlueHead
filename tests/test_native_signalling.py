"""Bumble signalling against the actual native HCI media fast path, offline."""
import asyncio
import time

import pytest
from bumble import avdtp, core

from bluehead.backend import Receiver, Settings
from bluehead.native_runtime import NativeClient, RemoteMixer
from bluehead.protocol import capabilities
from .test_protocols import Network, open_media
from .test_native_runtime import packet


class MediaIntercept:
    def __init__(self, client, host):
        self.client, self.host = client, host
        self.intercepted = 0

    def on_packet(self, data):
        raw = bytes(data)
        if raw[0] == 2:
            result = self.client.request("test_hci_acl", packet=raw[1:].hex())
            if result["handled"]:
                self.intercepted += 1
                return
        self.host.on_packet(raw)


@pytest.mark.asyncio
async def test_real_avdtp_registers_native_cid_before_audio_and_releases_it_on_close(tmp_path):
    network = Network(2)
    events = []
    with NativeClient(spawn=True) as client:
        mixer = RemoteMixer(client, buffer_ms=0)
        receiver = Receiver(Settings(calls=False, codec="sbc"), tmp_path,
                            lambda kind, **values: events.append((kind, values)), mixer)
        receiver.attach(network.devices[0])
        intercept = MediaIntercept(client, network.devices[0].host)
        network.devices[0].host.hci_sink.sink.set_packet_sink(intercept)
        await network.start()
        try:
            async with asyncio.timeout(15):
                connection, stream = await open_media(network.devices[1], network.devices[0], "sbc")
                key = next(iter(receiver.sessions))
                for i in range(45):
                    stream.rtp_channel.write(packet(i))
                    await asyncio.sleep(.003)
                assert intercept.intercepted > 40
                assert client.request("media_snapshot", key=key)["decoded_frames"] > 5000
                assert client.request("test_render", frames=480)["rms"] > .001
                await stream.stop()
                assert mixer.inputs[key].frames == 0
                with pytest.raises(core.ProtocolError):
                    await stream.protocol.send_command(avdtp.Reconfigure_Command(
                        stream.remote_endpoint.seid, [capabilities("aac", selected=True)]))
                assert client.request("media_snapshot", key=key)["codec"] == 1
                selected = capabilities("sbc", selected=True)
                information = bytearray(bytes(selected.media_codec_information))
                information[2] = 3
                selected = avdtp.MediaCodecCapabilities(selected.media_type, selected.media_codec_type, bytes(information))
                await stream.protocol.send_command(avdtp.Reconfigure_Command(stream.remote_endpoint.seid, [selected]))
                actual = await stream.protocol.get_configuration(stream.remote_endpoint.seid)
                assert bytes(next(c for c in actual if isinstance(c, avdtp.MediaCodecCapabilities)).media_codec_information) == bytes(information)
                await stream.start()
                for i in range(45):
                    stream.rtp_channel.write(packet(i))
                    await asyncio.sleep(.003)
                assert mixer.inputs[key].frames > 4000
                session = receiver.sessions[key]
                handle, cid = session.channel.connection.handle, session.channel.source_cid
                await stream.close()
                deadline = time.monotonic() + 1
                while session.channel is not None and time.monotonic() < deadline:
                    await asyncio.sleep(.01)
                assert session.channel is None
                # The old local CID is now available for future signalling.
                import struct
                payload = struct.pack("<HHHH", handle, 8, 4, cid) + b"test"
                assert not client.request("test_hci_acl", packet=payload.hex())["handled"]
                await connection.disconnect()
                await asyncio.sleep(.03)
                assert not receiver.sessions
        finally:
            for session in list(receiver.sessions.values()):
                session.close()
            await network.close()

"""LE Audio service capabilities derived from the native owner's actual probe."""

from dataclasses import dataclass
from bumble import gatt, hci
from bumble.profiles import bap, pacs
from .services import AudioStreamService


@dataclass(frozen=True)
class ControllerSupport:
    transport: str
    receive: bool
    transmit: bool
    broadcast: bool
    packet_length: int
    packets: int

    @classmethod
    def parse(cls, status):
        value = status.get("le_audio", {})
        transport = value.get("transport", "unavailable")
        known = transport in ("bulk_serialization", "intel_legacy_bulk") and value.get("features_probed") is True
        length, count = value.get("iso_packet_length", 0), value.get("iso_packets", 0)
        valid_buffers = type(length) is int and type(count) is int and 5 <= length <= 0x3FFF and 1 <= count <= 255
        receive = known and value.get("unicast_receive") is True
        transmit = receive and valid_buffers and value.get("unicast_transmit") is True and length * count - 4 >= 20
        return cls(
            transport,
            receive,
            transmit,
            known and value.get("broadcast_receive") is True,
            length if valid_buffers else 0,
            count if valid_buffers else 0,
        )

    @property
    def source_octets(self):
        return min(400, self.packet_length * self.packets - 4) if self.transmit else 0

    @property
    def source_blocks(self):
        return min(2, (self.packet_length * self.packets - 4) // self.source_octets) if self.transmit else 0


class CommonAudioService(gatt.TemplateService):
    UUID = gatt.GATT_COMMON_AUDIO_SERVICE

    def __init__(self):
        # This virtual single audio device is not a coordinated set of earbuds.
        super().__init__([])


def lc3_record(*, microphone=False, max_octets=400, blocks=2):
    if not 20 <= max_octets <= 400 or blocks not in (1, 2):
        raise ValueError("Invalid LC3 published capability limits")
    capabilities = bap.CodecSpecificCapabilities(
        supported_sampling_frequencies=bap.SupportedSamplingFrequency.from_hz((8000, 16000, 24000, 32000, 48000)),
        supported_frame_durations=(
            bap.SupportedFrameDuration.DURATION_7500_US_SUPPORTED
            | bap.SupportedFrameDuration.DURATION_10000_US_SUPPORTED
            | bap.SupportedFrameDuration.DURATION_10000_US_PREFERRED
        ),
        supported_audio_channel_count=(1,) if microphone else (1, 2),
        min_octets_per_codec_frame=20,
        max_octets_per_codec_frame=max_octets,
        supported_max_codec_frames_per_sdu=blocks * (1 if microphone else 2),
    )
    return pacs.PacRecord(hci.CodingFormat(hci.CodecID.LC3), capabilities)


@dataclass
class ServiceBundle:
    capabilities: pacs.PublishedAudioCapabilitiesService
    streams: AudioStreamService
    common: CommonAudioService

    @property
    def services(self):
        return (self.capabilities, self.streams, self.common)

    async def close(self):
        await self.streams.close()


def create_services(
    device, bridge, support: ControllerSupport, *, microphone=True, maximum_peers=3, emit=lambda *a, **k: None
):
    if not support.receive:
        raise ValueError("This adapter has no verified LE Audio receive transport")
    source = microphone and support.transmit
    contexts = bap.ContextType(0x0FFF)
    capabilities = pacs.PublishedAudioCapabilitiesService(
        contexts if source else bap.ContextType(0),
        contexts,
        contexts if source else bap.ContextType(0),
        contexts,
        sink_pac=[lc3_record()],
        sink_audio_locations=bap.AudioLocation.FRONT_LEFT | bap.AudioLocation.FRONT_RIGHT,
        source_pac=[lc3_record(microphone=True, max_octets=support.source_octets, blocks=support.source_blocks)]
        if source
        else (),
        source_audio_locations=bap.AudioLocation.FRONT_CENTER if source else None,
    )
    streams = AudioStreamService(
        device,
        bridge,
        microphone=source,
        maximum_peers=maximum_peers,
        emit=emit,
        source_octets=support.source_octets if source else 400,
        source_blocks=support.source_blocks if source else 2,
    )
    return ServiceBundle(capabilities, streams, CommonAudioService())

"""A2DP capability negotiation and bounded media packet extraction.

Capability layouts follow Bumble/AOSP. No unsupported codec is advertised.
"""
from dataclasses import dataclass
import struct

from bumble import a2dp, avdtp


class PacketError(ValueError):
    pass


@dataclass(frozen=True)
class Format:
    codec: str
    rate: int
    channels: int


def capabilities(codec: str, *, selected: bool = False):
    aac_limit = 192000 if codec == "aac_192" else 320000
    codec = "aac" if codec == "aac_192" else codec
    sbc, aac = a2dp.SbcMediaCodecInformation, a2dp.AacMediaCodecInformation
    rate_sbc = sbc.SamplingFrequency.SF_48000 | sbc.SamplingFrequency.SF_44100
    rate_aac = aac.SamplingFrequency.SF_48000 | aac.SamplingFrequency.SF_44100
    if selected:
        rate_sbc, rate_aac = sbc.SamplingFrequency.SF_48000, aac.SamplingFrequency.SF_48000
    if codec == "sbc":
        info = sbc(
            rate_sbc,
            sbc.ChannelMode.JOINT_STEREO if selected else sbc.ChannelMode(15),
            sbc.BlockLength.BL_16 if selected else sbc.BlockLength(15),
            sbc.Subbands.S_8 if selected else sbc.Subbands(3),
            sbc.AllocationMethod.LOUDNESS if selected else sbc.AllocationMethod(3),
            2, 53,
        )
        codec_type = a2dp.CodecType.SBC
    elif codec == "aac":
        info = aac(
            aac.ObjectType.MPEG_4_AAC_LC if selected else (
                aac.ObjectType.MPEG_4_AAC_LC | aac.ObjectType.MPEG_2_AAC_LC
            ), rate_aac, aac.Channels.STEREO, 1, aac_limit,
        )
        codec_type = a2dp.CodecType.MPEG_2_4_AAC
    else:
        vendor, identifier, data = {
            "aptx": (0x4F, 1, bytes([0x12 if selected else 0x32])),
            "aptx_hd": (0xD7, 0x24, bytes([0x12 if selected else 0x32, 0, 0, 0, 0])),
            "ldac": (0x12D, 0xAA, bytes([0x10 if selected else 0x3C, 1])),
        }[codec]
        info = a2dp.VendorSpecificMediaCodecInformation(vendor, identifier, data)
        codec_type = a2dp.CodecType.NON_A2DP
    return avdtp.MediaCodecCapabilities(avdtp.MediaType.AUDIO, codec_type, info)


def negotiated(codec: str, config) -> Format:
    match = [c for c in config if isinstance(c, avdtp.MediaCodecCapabilities)]
    if len(match) != 1:
        raise PacketError("Expected one negotiated media codec")
    offered = capabilities(codec)
    aac_limit = 192000 if codec == "aac_192" else 320000
    codec = "aac" if codec == "aac_192" else codec
    cap = match[0]
    raw = bytes(cap.media_codec_information)
    offered_raw = bytes(offered.media_codec_information)
    if cap.media_codec_type != offered.media_codec_type or len(raw) != len(offered_raw):
        raise PacketError("Codec type/length differs from the advertised endpoint")
    # Every selected mask must contain one supported value, not a capability set.
    def choose(value, mapping):
        if value not in mapping:
            raise PacketError(f"Unsupported or ambiguous selected value: {value}")
        return mapping[value]
    if codec == "sbc":
        rate = choose(raw[0] >> 4, {1: 48000, 2: 44100})
        channels = choose(raw[0] & 15, {1: 2, 2: 2, 4: 2, 8: 1})
        choose(raw[1] >> 4, {1: 16, 2: 12, 4: 8, 8: 4})
        choose((raw[1] >> 2) & 3, {1: 8, 2: 4})
        choose(raw[1] & 3, {1: 1, 2: 2})
        if not 2 <= raw[2] <= raw[3] <= 53:
            raise PacketError("Unsupported SBC bitpool")
    elif codec == "aac":
        choose(raw[0], {0x80: 1, 0x40: 1})
        rate = choose((raw[1] << 4) | (raw[2] >> 4), {8: 48000, 16: 44100})
        channels = choose((raw[2] >> 2) & 3, {1: 2})
        if (int.from_bytes(raw[3:], "big") & 0x7FFFFF) > aac_limit:
            raise PacketError("AAC bitrate exceeds advertised maximum")
    else:
        if raw[:6] != offered_raw[:6]:
            raise PacketError("Vendor codec identifier mismatch")
        if codec == "ldac":
            rate = choose(raw[6], {0x20: 44100, 0x10: 48000, 8: 88200, 4: 96000})
            channels = choose(raw[7], {1: 2})
        else:
            rate = choose(raw[6] >> 4, {1: 48000, 2: 44100})
            channels = choose(raw[6] & 15, {2: 2})
    return Format(codec, rate, channels)


@dataclass(frozen=True)
class Rtp:
    sequence: int
    timestamp: int
    marker: bool
    payload: bytes


def parse_rtp(data: bytes) -> Rtp:
    if not 12 <= len(data) <= 65535 or data[0] >> 6 != 2:
        raise PacketError("Invalid RTP header")
    offset = 12 + 4 * (data[0] & 15)
    if offset > len(data):
        raise PacketError("Truncated RTP CSRC list")
    if data[0] & 0x10:
        if offset + 4 > len(data):
            raise PacketError("Truncated RTP extension")
        offset += 4 + 4 * int.from_bytes(data[offset + 2:offset + 4], "big")
    end = len(data)
    if data[0] & 0x20:
        padding = data[-1]
        if not padding or padding > end - offset:
            raise PacketError("Invalid RTP padding")
        end -= padding
    if offset >= end:
        raise PacketError("Empty/truncated RTP payload")
    sequence, timestamp = struct.unpack_from(">HI", data, 2)
    return Rtp(sequence, timestamp, bool(data[1] & 0x80), data[offset:end])


def ldac_frames(payload: bytes, fmt: Format) -> list[bytes]:
    if not payload or payload[0] & 0xE0:
        raise PacketError("Invalid/fragmented LDAC media header")
    count, offset, frames = payload[0] & 31, 1, []
    if count == 0:
        raise PacketError("Zero LDAC frames")
    for _ in range(count):
        if offset + 3 > len(payload) or payload[offset] != 0xAA:
            raise PacketError("Invalid LDAC frame sync")
        header = int.from_bytes(payload[offset:offset + 3], "big")
        rate_id, channel_id = (header >> 13) & 7, (header >> 11) & 3
        if rate_id >= 4 or channel_id != 2:
            raise PacketError("Unsupported LDAC format")
        if (44100, 48000, 88200, 96000)[rate_id] != fmt.rate:
            raise PacketError("LDAC frame rate differs from negotiated rate")
        length = ((header >> 2) & 511) + 4
        if offset + length > len(payload):
            raise PacketError("Truncated LDAC frame")
        frames.append(payload[offset:offset + length])
        offset += length
    if offset != len(payload):
        raise PacketError("LDAC frame count/trailing bytes mismatch")
    return frames


class Depacketizer:
    def __init__(self, fmt: Format):
        self.fmt = fmt
        self.last_sequence = None
        self.lost = 0
        self.fragment = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        if self.fmt.codec == "aptx":
            if not data or len(data) > 65535 or len(data) % 4:
                raise PacketError("Invalid raw aptX packet size")
            return [data]
        packet = parse_rtp(data)
        if self.last_sequence is not None:
            distance = (packet.sequence - self.last_sequence) & 0xFFFF
            if distance == 0 or distance > 0x8000:
                return []  # duplicate or late packet
            if distance > 1:
                self.lost += distance - 1
                self.fragment.clear()
        self.last_sequence = packet.sequence
        payload = packet.payload
        if self.fmt.codec == "sbc":
            if payload[0] & 0x80:
                if payload[0] & 0x40:
                    self.fragment = bytearray(payload[1:])
                elif self.fragment:
                    self.fragment.extend(payload[1:])
                else:
                    raise PacketError("Orphan SBC fragment")
                if len(self.fragment) > 4096:
                    self.fragment.clear()
                    raise PacketError("SBC fragment too large")
                if payload[0] & 0x20:
                    result = bytes(self.fragment)
                    self.fragment.clear()
                    return [result]
                return []
            if payload[0] & 15 == 0 or len(payload) < 5:
                raise PacketError("Invalid SBC frame count")
            return [payload[1:]]
        if self.fmt.codec == "aac":
            # A2DP uses LATM AudioMuxElement; FFmpeg maintains useSameStreamMux state.
            if len(payload) > 8191:
                raise PacketError("AAC LATM element too large")
            return [bytes([0x56, 0xE0 | (len(payload) >> 8), len(payload) & 255]) + payload]
        if self.fmt.codec == "aptx_hd":
            if len(payload) < 6 or len(payload) % 6:
                raise PacketError("Invalid aptX HD payload size")
            return [payload]
        return ldac_frames(payload, self.fmt)

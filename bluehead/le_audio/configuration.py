"""Strict LC3 negotiation before allocating native streams.

BAP 4.3.2: frequency, duration and octets are mandatory; omitted channel
allocation means mono and omitted blocks-per-SDU means one. Codec configuration
is distinct from metadata, whose tags may repeat.
"""

from dataclasses import dataclass
from bumble.profiles.ascs import AseReasonCode as Reason, AseResponseCode as Response


class ConfigurationError(ValueError):
    def __init__(self, detail, reason=Reason.CODEC_SPECIFIC_CONFIGURATION, *, unsupported=False):
        super().__init__(detail)
        self.reason = reason
        self.response = (
            Response.UNSUPPORTED_CONFIGURATION_PARAMETER_VALUE
            if unsupported
            else Response.INVALID_CONFIGURATION_PARAMETER_VALUE
        )


def ltv_fields(data: bytes) -> dict[int, bytes]:
    if not isinstance(data, bytes) or len(data) > 255:
        raise ConfigurationError("Invalid LC3 configuration envelope")
    fields = {}
    position = 0
    while position < len(data):
        length = data[position]
        end = position + 1 + length
        if length < 1 or end > len(data):
            raise ConfigurationError("Truncated or empty LC3 configuration field")
        tag = data[position + 1]
        if tag in (1, 2, 3, 4, 5):
            if tag in fields:
                raise ConfigurationError("Duplicate LC3 configuration field")
            fields[tag] = data[position + 2 : end]
        position = end
    return fields


@dataclass(frozen=True)
class Lc3Configuration:
    rate: int
    duration_us: int
    channels: int
    octets: int
    blocks: int = 1
    allocation: int = 0

    @property
    def sdu_bytes(self):
        return self.channels * self.octets * self.blocks

    @property
    def interval_us(self):
        return self.duration_us * self.blocks

    @property
    def samples(self):
        return self.rate * self.duration_us // 1_000_000

    @property
    def bitrate(self):
        return self.octets * self.channels * 8_000_000 // self.duration_us

    @classmethod
    def parse(cls, data: bytes, *, source=False):
        fields = ltv_fields(data)
        widths = {1: 1, 2: 1, 3: 4, 4: 2, 5: 1}
        if not {1, 2, 4}.issubset(fields):
            raise ConfigurationError("Missing mandatory LC3 configuration field")
        for tag, width in widths.items():
            if tag in fields and len(fields[tag]) != width:
                raise ConfigurationError("Wrong LC3 configuration field width")
        values = {tag: int.from_bytes(value, "little") for tag, value in fields.items()}
        rates = {1: 8000, 3: 16000, 5: 24000, 6: 32000, 8: 48000}
        if values[1] not in rates or values[2] not in (0, 1):
            raise ConfigurationError("Unsupported LC3 rate or frame duration", unsupported=True)
        allocation = values.get(3, 0)
        channels = allocation.bit_count() or 1
        locations = 7 if source else 3
        if allocation & ~locations or channels > (1 if source else 2):
            raise ConfigurationError("Unsupported LC3 audio allocation", unsupported=True)
        octets, blocks = values[4], values.get(5, 1)
        if not 20 <= octets <= 400 or blocks not in (1, 2):
            raise ConfigurationError("Unsupported LC3 frame size", unsupported=True)
        return cls(rates[values[1]], 7500 if values[2] == 0 else 10000, channels, octets, blocks, allocation)


@dataclass(frozen=True)
class QosConfiguration:
    cig: int
    cis: int
    interval_us: int
    framing: int
    phy: int
    max_sdu: int
    retransmissions: int
    max_latency_ms: int
    presentation_us: int

    def validate(self, codec: Lc3Configuration):
        def require(condition, detail, reason):
            if not condition:
                raise ConfigurationError(detail, reason, unsupported=True)

        require(
            type(self.cig) is int and type(self.cis) is int and 0 <= self.cig <= 0xEF and 0 <= self.cis <= 0xEF,
            "Invalid CIG/CIS mapping",
            Reason.INVALID_ASE_CIS_MAPPING,
        )
        require(
            type(self.interval_us) is int and self.interval_us == codec.interval_us,
            "SDU interval differs from codec frame blocks",
            Reason.SDU_INTERVAL,
        )
        require(type(self.framing) is int and self.framing in (0, 1), "Unsupported ISO framing", Reason.FRAMING)
        require(type(self.phy) is int and self.phy in (1, 2), "Unsupported ISO PHY", Reason.PHY)
        require(
            type(self.max_sdu) is int and codec.sdu_bytes <= self.max_sdu <= 4095,
            "ISO SDU capacity does not fit codec configuration",
            Reason.MAXIMUM_SDU_SIZE,
        )
        require(
            type(self.retransmissions) is int and 0 <= self.retransmissions <= 15,
            "Unsupported retransmission count",
            Reason.RETRANSMISSION_NUMBER,
        )
        require(
            type(self.max_latency_ms) is int and 5 <= self.max_latency_ms <= 100,
            "Transport latency exceeds the exposed limit",
            Reason.MAX_TRANSPORT_LATENCY,
        )
        require(
            type(self.presentation_us) is int and 10_000 <= self.presentation_us <= 80_000,
            "Presentation delay exceeds the exposed range",
            Reason.PRESENTATION_DELAY,
        )
        return self

"""Bounded BASE/advertising parsing and LC3 broadcast channel selection.

No controller, PCM or native resources. Unsupported codecs remain discoverable;
malformed announcements never allocate or partially replace a playing stream.
"""

from dataclasses import dataclass
from bumble.profiles.ascs import AseResponseCode as Response
from .configuration import ConfigurationError, Lc3Configuration, ltv_fields
from .ascs_control import metadata


class AnnouncementError(ValueError):
    pass


class Reader:
    def __init__(self, data, maximum=1650):
        if not isinstance(data, bytes) or len(data) > maximum:
            raise AnnouncementError("Invalid broadcast envelope")
        self.data, self.offset = data, 0

    def take(self, size):
        end = self.offset + size
        if size < 0 or end > len(self.data):
            raise AnnouncementError("Truncated broadcast field")
        value, self.offset = self.data[self.offset : end], end
        return value

    def integer(self, size=1):
        return int.from_bytes(self.take(size), "little")

    def sized(self):
        return self.take(self.integer())

    def finish(self):
        if self.offset != len(self.data):
            raise AnnouncementError("Unexpected trailing broadcast data")


def fields_bytes(fields):
    return b"".join(bytes([len(value) + 1, tag]) + value for tag, value in fields.items())


@dataclass(frozen=True)
class Bis:
    index: int
    configuration: bytes
    codec: Lc3Configuration | None


@dataclass(frozen=True)
class Subgroup:
    coding: bytes
    configuration: bytes
    metadata: bytes
    streams: tuple[Bis, ...]


@dataclass(frozen=True)
class Selection:
    subgroup: int
    streams: tuple[Bis, ...]
    codec: Lc3Configuration
    split: bool
    presentation_us: int

    @property
    def indices(self):
        return tuple(stream.index for stream in self.streams)

    @property
    def mask(self):
        return sum(1 << (stream.index - 1) for stream in self.streams)


@dataclass(frozen=True)
class Base:
    presentation_us: int
    subgroups: tuple[Subgroup, ...]

    @classmethod
    def parse(cls, data):
        reader = Reader(data)
        presentation, count = reader.integer(3), reader.integer()
        if not 1 <= count <= 31:
            raise AnnouncementError("Invalid broadcast subgroup count")
        groups, indices = [], set()
        try:
            for _ in range(count):
                bis_count, coding = reader.integer(), reader.take(5)
                configuration, meta = reader.sized(), metadata(reader.sized())
                if not 1 <= bis_count <= 31 or len(indices) + bis_count > 31:
                    raise AnnouncementError("Invalid broadcast stream count")
                common = ltv_fields(configuration)
                streams = []
                for _ in range(bis_count):
                    index, override = reader.integer(), reader.sized()
                    if not 1 <= index <= 31 or index in indices:
                        raise AnnouncementError("Duplicate or reserved BIS index")
                    indices.add(index)
                    fields = common | ltv_fields(override)
                    codec = None
                    if coding == b"\x06\x00\x00\x00\x00":
                        try:
                            codec = Lc3Configuration.parse(fields_bytes(fields))
                        except ConfigurationError as error:
                            if error.response != Response.UNSUPPORTED_CONFIGURATION_PARAMETER_VALUE:
                                raise
                    streams.append(Bis(index, override, codec))
                groups.append(Subgroup(coding, configuration, meta, tuple(streams)))
            reader.finish()
        except ValueError as error:
            raise AnnouncementError(str(error)) from error
        return cls(presentation, tuple(groups))

    def select(self, subgroup=None, masks=None):
        """Choose one coherent mono/stereo program, never mix languages/groups.

        Explicit requested masks must be completely supported. No-preference
        selects stereo when available, otherwise the first supported program.
        """
        choices = range(len(self.subgroups)) if subgroup is None else (subgroup,)
        if masks is not None and len(masks) != len(self.subgroups):
            raise AnnouncementError("BIS masks differ from BASE subgroup count")
        for index in choices:
            if type(index) is not int or not 0 <= index < len(self.subgroups):
                raise AnnouncementError("Unknown broadcast subgroup")
            group = self.subgroups[index]
            mask = 0xFFFFFFFF if masks is None else masks[index]
            if type(mask) is not int or not 0 <= mask <= 0xFFFFFFFF or mask == 0:
                continue
            available = sum(1 << (stream.index - 1) for stream in group.streams)
            if mask != 0xFFFFFFFF and mask & ~available:
                raise AnnouncementError("Requested BIS is absent from subgroup")
            candidates = [
                stream for stream in group.streams if mask == 0xFFFFFFFF or mask & (1 << (stream.index - 1))
            ]
            if mask != 0xFFFFFFFF and (len(candidates) > 2 or any(stream.codec is None for stream in candidates)):
                raise AnnouncementError("Requested broadcast streams are unsupported")
            supported = [stream for stream in candidates if stream.codec is not None]
            # A two-channel BIS already contains a synchronized stereo program.
            stereo = next((stream for stream in supported if stream.codec.channels == 2), None)
            if stereo is not None and (mask == 0xFFFFFFFF or len(candidates) == 1):
                return Selection(index, (stereo,), stereo.codec, False, self.presentation_us)
            left = next((stream for stream in supported if stream.codec.allocation == 1), None)
            right = next((stream for stream in supported if stream.codec.allocation == 2), None)
            if left and right:
                a, b = left.codec, right.codec
                if (a.rate, a.duration_us, a.octets, a.blocks) == (b.rate, b.duration_us, b.octets, b.blocks):
                    return Selection(index, (left, right), a, True, self.presentation_us)
            if len(candidates) == 1 or mask == 0xFFFFFFFF:
                mono = next((stream for stream in supported if stream.codec.channels == 1), None)
                if mono is not None:
                    return Selection(index, (mono,), mono.codec, False, self.presentation_us)
            if mask != 0xFFFFFFFF:
                raise AnnouncementError("Selected BIS channels do not form one synchronized program")
        raise AnnouncementError("No supported LC3 audio program in this broadcast")


def service_data(data, service):
    """Strict AD structure parsing; reject a truncated report, not just its tail."""
    reader = Reader(data)
    found = None
    while reader.offset < len(data):
        size = reader.integer()
        if not size:
            # HCI reports may include zero padding but not hidden extra fields.
            if any(reader.take(len(data) - reader.offset)):
                raise AnnouncementError("Nonzero data after advertising terminator")
            break
        kind, payload = reader.integer(), reader.take(size - 1)
        if kind == 0x16 and len(payload) >= 2 and int.from_bytes(payload[:2], "little") == service:
            if found is not None:
                raise AnnouncementError("Duplicate audio service data")
            found = payload[2:]
    return found

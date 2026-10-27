"""Strict BASS operations. Parse the whole write before applying effects."""

from dataclasses import dataclass
from bumble import att, hci
from .broadcast import Reader, AnnouncementError
from .ascs_control import metadata


@dataclass(frozen=True)
class SubgroupRequest:
    mask: int
    metadata: bytes


@dataclass(frozen=True)
class Operation:
    opcode: int
    source: int = 0
    address: object = None
    sid: int = 0
    broadcast: int = 0
    pa_sync: int = 0
    interval: int = 0xFFFF
    subgroups: tuple[SubgroupRequest, ...] = ()
    code: bytes = b""


def parse_operation(data):
    reader = Reader(data, maximum=512)
    opcode = reader.integer()
    if opcode > 5:
        raise att.ATT_Error(0x80)
    source, address, sid, broadcast, sync, interval, groups, code = 0, None, 0, 0, 0, 0xFFFF, (), b""
    if opcode == 2:
        address_type, address_bytes = reader.integer(), reader.take(6)
        sid, broadcast = reader.integer(), reader.integer(3)
        if address_type not in (0, 1) or not 0 <= sid <= 15:
            raise AnnouncementError("Invalid broadcast address or SID")
        address = hci.Address(address_bytes, address_type=address_type)
    elif opcode in (3, 4, 5):
        source = reader.integer()
    if opcode in (2, 3):
        sync, interval, count = reader.integer(), reader.integer(2), reader.integer()
        if sync not in (0, 1, 2) or not 0 <= count <= 31 or (interval != 0xFFFF and interval < 6):
            raise AnnouncementError("Invalid periodic advertising request")
        parsed = []
        for _ in range(count):
            mask, meta = reader.integer(4), reader.sized()
            if mask != 0xFFFFFFFF and mask & 0x80000000:
                raise AnnouncementError("Reserved BIS request bit")
            parsed.append(SubgroupRequest(mask, metadata(meta)))
        groups = tuple(parsed)
    if opcode == 4:
        code = reader.take(16)
    reader.finish()
    return Operation(opcode, source, address, sid, broadcast, sync, interval, groups, code)

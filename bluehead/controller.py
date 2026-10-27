"""Versioned adapter selectors; physical identity is resolved by the C++ owner."""
import re

IDENTITY = re.compile(r"usb-id:([0-9a-f]{24})(?:\+sco=0)?")
LEGACY = re.compile(r"usb:([0-9a-fA-F]{4}):([0-9a-fA-F]{4})#(\d{1,2})(?:\+sco=0)?")


def transport_target(spec):
    if not isinstance(spec, str):
        raise ValueError("请选择有效蓝牙适配器")
    if match := IDENTITY.fullmatch(spec):
        return {"adapter_id": match[1]}
    if match := LEGACY.fullmatch(spec):
        return {"vid": int(match[1], 16), "pid": int(match[2], 16), "index": int(match[3])}
    raise ValueError("请选择有效蓝牙适配器")

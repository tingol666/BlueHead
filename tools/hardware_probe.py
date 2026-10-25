"""Initialize the explicitly selected adapter; make no peer connections or calls."""
import asyncio
import json
import logging
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bumble.device import Device, DeviceConfiguration
from bluehead.usb_transport import open_transport


async def probe(spec):
    result = {"passed": False, "transport": spec, "scope": "controller and USB voice interface only"}
    try:
        async with await open_transport(spec) as transport:
            config = DeviceConfiguration()
            config.name = "BlueHead PC"
            config.classic_enabled = True
            config.le_enabled = False
            device = Device.from_config_with_hci(config, transport.source, transport.sink)
            await asyncio.wait_for(device.power_on(), 30)
            version = device.host.local_version
            result["version"] = {key: int(getattr(version, key)) for key in (
                "hci_version", "hci_revision", "lmp_version", "lmp_subversion", "company_identifier",
            ) if hasattr(version, key)}
            result["lmp_features"] = int(device.host.local_lmp_features)
            result["usb_voice_settings"] = {
                str(index): list(endpoints.max_packet_sizes)
                for index, endpoints in getattr(transport, "sco_settings", {}).items()
            }
            result["voice_interface_claimed"] = getattr(transport, "sco_interface", None) is not None
            await asyncio.wait_for(device.power_off(), 5)
            result["passed"] = True
    except Exception:
        result["error"] = traceback.format_exc()
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, filename="work/hardware-probe.log", encoding="utf-8")
    report = asyncio.run(probe(sys.argv[1]))
    Path(sys.argv[2]).write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    raise SystemExit(0 if report["passed"] else 1)

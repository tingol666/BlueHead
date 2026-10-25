"""Explicit-adapter USB ISO check; no phone links, microphone or saved audio."""
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from bluehead.usb_check import probe


if __name__ == "__main__":
    if len(sys.argv) != 3 or "+sco=0" not in sys.argv[1]:
        raise SystemExit("Usage: python tools/usb_voice_probe.py usb:VID:PID#INDEX+sco=0 REPORT.json")
    report = asyncio.run(probe(sys.argv[1]))
    Path(sys.argv[2]).write_text(json.dumps(report, indent=2), "utf-8")
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["passed"] else 1)

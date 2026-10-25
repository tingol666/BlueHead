"""Bounded, local HFP diagnostic. Records protocol state/counts, never audio."""
import asyncio
from dataclasses import fields
import json
import logging
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class MetadataOnly(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if record.name.startswith("bluehead"):
            return True
        if record.name == "bumble.hfp":
            if message.startswith(">>> "):
                return message[4:].startswith(("AT+BRSF", "AT+BAC", "AT+BCS", "AT+BCC", "AT+CIND", "AT+CMER"))
            if message.startswith("<<< "):
                return message[4:].startswith(("+BRSF", "+BCS", "+CIND", "+CIEV", "OK", "ERROR", "+CME ERROR"))
            return record.levelno >= logging.INFO
        if record.name == "bumble.host":
            return any(word in message for word in ("SYNCHRONOUS", "CONNECTION_REQUEST_EVENT", "DISCONNECTION_COMPLETE_EVENT"))
        if record.name == "bumble.transport.usb":
            return message.startswith(("SCO ", "switching SCO", "starting SCO"))
        return record.levelno >= logging.WARNING


async def run(address):
    from bumble import hci
    from bumble.core import PhysicalTransport
    from bluehead.backend import Receiver, Settings

    root = Path(__file__).resolve().parents[1]
    ready = asyncio.Event()
    events = []

    def emit(kind, **data):
        events.append({"time": time.time(), "kind": kind, **data})
        logging.getLogger("bluehead.probe").info("%s %s", kind, data)
        if kind == "status" and data.get("message", "").startswith("已就绪"):
            ready.set()

    settings_data = json.loads((root / "data/settings.json").read_text("utf-8"))
    settings = Settings(**{k: v for k, v in settings_data.items() if k in {f.name for f in fields(Settings)}})
    receiver = Receiver(settings, root / "data", emit)
    task = asyncio.create_task(receiver.run())
    report = {"scope": "HFP protocol/USB metadata only; no audio file", "events": events}
    try:
        await asyncio.wait_for(ready.wait(), 35)
        peer = hci.Address(address, hci.Address.PUBLIC_DEVICE_ADDRESS)
        connection = await asyncio.wait_for(receiver.device.connect(peer, transport=PhysicalTransport.BR_EDR), 20)
        key = str(connection.peer_address)
        await asyncio.sleep(1)
        protocol = await receiver.calls.ensure_control(key)
        report["ag_features"] = protocol.supported_ag_features
        report["service_ready"] = protocol._slc_initialized
        try:
            await receiver.calls.action("call_audio", key)
            report["audio_request"] = "accepted"
        except Exception as exc:
            report["audio_request"] = repr(exc)
        for _ in range(90):
            report["voice"] = receiver.calls.statistics
            report["microphone_frames"] = getattr(receiver.calls.microphone, "frames", 0)
            report["active_links"] = len(receiver.calls.links)
            (root / "work/voice-probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
            if (root / "work/voice-probe.stop").exists():
                break
            await asyncio.sleep(1)
    except Exception as exc:
        report["error"] = repr(exc)
        logging.getLogger("bluehead.probe").exception("voice probe failed")
    finally:
        receiver.stop_event.set()
        await task
        (root / "work/voice-probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "events"}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    handler = logging.FileHandler("work/voice-probe.log", encoding="utf-8", mode="w")
    handler.addFilter(MetadataOnly())
    logging.basicConfig(level=logging.DEBUG, handlers=[handler])
    asyncio.run(run(sys.argv[1]))

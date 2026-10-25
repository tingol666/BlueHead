"""Normal desktop UI with local metadata diagnostics; does not record audio."""
import json
import logging
from logging.handlers import RotatingFileHandler
import multiprocessing
from pathlib import Path
import sys
import time
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from bluehead.app import App
    from tools.voice_probe import MetadataOnly

    root_path = Path(__file__).resolve().parents[1]
    work = root_path / "work"
    handler = RotatingFileHandler(work / "live-receiver.log", maxBytes=1000000, backupCount=2, encoding="utf-8")
    handler.addFilter(MetadataOnly())
    logging.basicConfig(level=logging.DEBUG, handlers=[handler])
    logging.getLogger("bluehead").setLevel(logging.INFO)
    events = (work / "acceptance-events.jsonl").open("a", encoding="utf-8", buffering=1)

    class ObservedApp(App):
        def emit(self, kind, **values):
            if kind != "inventory":
                events.write(json.dumps({"time": time.time(), "kind": kind, **values}, ensure_ascii=False) + "\n")
            super().emit(kind, **values)

    root = tk.Tk()
    app = ObservedApp(root, root_path / "data")

    def start_when_ready():
        if app.adapters and app.audio_inventory_loaded:
            app.start()
        else:
            root.after(200, start_when_ready)

    if "--start" in sys.argv:
        root.after(300, start_when_ready)

    last_command = None

    def snapshot():
        nonlocal last_command
        report = {"time": time.time(), "status": app.status.get(), "running": app.running, "streams": {}}
        receiver = app.runner.receiver
        if receiver:
            command_path = work / "live-command.json"
            if command_path.exists():
                command = json.loads(command_path.read_text("utf-8"))
                if command.get("id") != last_command:
                    last_command = command.get("id")
                    if command.get("action") == "request_audio" and receiver.connections:
                        app.runner.action("call_audio", next(iter(receiver.connections)))
            for key, session in list(receiver.sessions.items()):
                report["streams"][key] = {
                    "codec": session.format.codec if session.format else None,
                    "sample_rate": session.format.rate if session.format else None,
                    "packets": session.packets,
                    "decoded_frames": session.decoded_frames,
                    "decode_errors": session.errors,
                    "queue_resets": session.drops,
                }
            report["mixer_underruns"] = receiver.mixer.underruns
            if receiver.calls:
                report["call_control_connections"] = len(receiver.calls.protocols)
                report["call_audio_connections"] = len(receiver.calls.links)
                report["microphone_active"] = receiver.calls.microphone is not None
                report["call_audio_statistics"] = receiver.calls.statistics
                report["microphone_frames"] = getattr(receiver.calls.microphone, "frames", 0)
                report["microphone_device"] = receiver.calls.input_device
            report["output_device"] = receiver.settings.output_device
            report["output_underflows"] = getattr(receiver.output, "underflows", 0)
        temporary = work / "live-status.tmp"
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        temporary.replace(work / "live-status.json")
        root.after(1000, snapshot)

    root.after(1000, snapshot)
    root.mainloop()
    events.close()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()

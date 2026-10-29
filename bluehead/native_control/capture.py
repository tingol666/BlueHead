"""Opt-in control-plane capture at the existing native transport adapter.

No new USB consumer or ISO/PCM path. Signalling (including pairing secrets)
is kept verbatim only when explicitly enabled for a local diagnostic run.
The writer never blocks a protocol callback; loss/limits are reported.
"""

import datetime
import json
import os
from pathlib import Path
import queue
import struct
import threading
import time

from bumble.snoop import BtSnooper


class ControlCapture:
    def __init__(self, directory, *, maximum_bytes=256 * 1024 * 1024):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"hci-{time.time_ns()}-{os.getpid()}"
        self.path = directory / (stem + ".btsnoop")
        self.metadata = directory / (stem + ".jsonl")
        self.maximum_bytes = maximum_bytes
        self.queue = queue.Queue(maxsize=4096)
        self.closing = threading.Event()
        self.dropped = self.written = 0
        self.error = None
        self.last_batch = 0
        epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)
        self.anchor = BtSnooper.TIMESTAMP_DELTA - int((BtSnooper.TIMESTAMP_ANCHOR - epoch).total_seconds() * 1e6)
        self.thread = threading.Thread(target=self.write, name="hci-diagnostic-writer", daemon=True)
        self.thread.start()

    @classmethod
    def configured(cls):
        directory = os.environ.get("BLUEHEAD_HCI_TRACE_DIR")
        return cls(directory) if directory else None

    def put(self, item):
        if self.closing.is_set() or self.error:
            return
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            self.dropped += 1

    def packet(self, wire, direction):
        self.put(("packet", time.time_ns() // 1000 + self.anchor, bytes(wire), direction))

    def batch(self, value):
        now = time.monotonic()
        if now - self.last_batch >= .5 or value.get("error") or not value.get("active", True):
            self.last_batch = now
            self.put(("status", time.time_ns(), {k: v for k, v in value.items() if k not in ("packets", "packet_epochs")}))

    def write(self):
        try:
            with self.path.open("xb") as wire, self.metadata.open("x", encoding="utf-8") as metadata:
                wire.write(b"btsnoop\0" + struct.pack(">II", 1, 1002))
                metadata.write(json.dumps({"scope": "Complete HCI signalling at the existing adapter; native realtime ISO/SCO bypass Python", "contains_pairing_secrets": True}) + "\n")
                while not self.closing.is_set() or not self.queue.empty():
                    try:
                        item = self.queue.get(timeout=.1)
                    except queue.Empty:
                        wire.flush()
                        metadata.flush()
                        continue
                    if item[0] == "packet":
                        _, stamp, packet, direction = item
                        if self.written + len(packet) + 24 > self.maximum_bytes:
                            self.dropped += 1
                            continue
                        flags = int(direction) | (2 if packet and packet[0] in (1, 4) else 0)
                        wire.write(struct.pack(">IIIIQ", len(packet), len(packet), flags, self.dropped, stamp) + packet)
                        self.written += len(packet) + 24
                    else:
                        metadata.write(json.dumps({"time_ns": item[1], "status": item[2]}, ensure_ascii=False) + "\n")
                metadata.write(json.dumps({"complete": True, "dropped": self.dropped, "bytes": self.written}) + "\n")
        except Exception as error:
            self.error = type(error).__name__

    def close(self):
        self.closing.set()
        self.thread.join(3)
        return {"path": str(self.path), "dropped": self.dropped, "error": self.error,
                "writer_stopped": not self.thread.is_alive()}

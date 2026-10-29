"""Local diagnostic filter: protocol metadata only, no HCI audio/key payloads."""
import logging
import asyncio
import sys
import threading
import time
import traceback

log = logging.getLogger(__name__)


class LoopWatchdog:
    """Capture Python stacks once per stalled interval; never capture locals."""
    def __init__(self, timeout=6):
        self.timeout = timeout
        self.last_beat = time.monotonic()
        self.stopped = threading.Event()
        self.task = None

    def start(self):
        async def heartbeat():
            while True:
                self.last_beat = time.monotonic()
                await asyncio.sleep(.5)
        self.task = asyncio.create_task(heartbeat())
        threading.Thread(target=self.watch, daemon=True, name="receiver-watchdog").start()

    def watch(self):
        reported = False
        while not self.stopped.wait(1):
            stalled = time.monotonic() - self.last_beat >= self.timeout
            if stalled and not reported:
                for thread in threading.enumerate():
                    frame = sys._current_frames().get(thread.ident)
                    if frame is not None:
                        log.warning("Receiver stalled: thread=%s\n%s", thread.name,
                                    "".join(traceback.format_stack(frame, limit=12)))
            reported = stalled

    def close(self):
        self.stopped.set()
        if self.task:
            self.task.cancel()


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
            header = message.splitlines()[0] if message else ""
            return any(name in header for name in (
                "HCI_CONNECTION_REQUEST_EVENT:", "HCI_SYNCHRONOUS_CONNECTION_COMPLETE_EVENT:",
                "HCI_SYNCHRONOUS_CONNECTION_CHANGED_EVENT:", "HCI_DISCONNECTION_COMPLETE_EVENT:",
                "HCI_COMMAND_STATUS_EVENT:", "HCI_ACCEPT_SYNCHRONOUS_CONNECTION_REQUEST_COMMAND:",
                "HCI_ENHANCED_ACCEPT_SYNCHRONOUS_CONNECTION_REQUEST_COMMAND:",
                "HCI_SETUP_SYNCHRONOUS_CONNECTION_COMMAND:", "HCI_ENHANCED_SETUP_SYNCHRONOUS_CONNECTION_COMMAND:",
                "HCI_LE_CIS_REQUEST_EVENT:", "HCI_LE_CIS_ESTABLISHED_EVENT:",
                "HCI_LE_ACCEPT_CIS_REQUEST_COMMAND:", "HCI_LE_REJECT_CIS_REQUEST_COMMAND:",
                "HCI_LE_SETUP_ISO_DATA_PATH_COMMAND:", "HCI_LE_REMOVE_ISO_DATA_PATH_COMMAND:",
            ))
        if record.name == "bumble.transport.usb":
            return message.startswith(("SCO alternate settings", "SCO OUT: alternate setting", "SCO link",
                                       "switching SCO", "starting SCO", "USB ISO", "USB event loop"))
        return record.levelno >= logging.WARNING

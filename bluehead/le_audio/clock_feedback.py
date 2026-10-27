"""HCI ISO TX Sync compatibility and sparse native cadence feedback control."""

import asyncio
from dataclasses import dataclass, field
import logging
from bumble import hci

log = logging.getLogger(__name__)


@dataclass
class IsoTxSyncReturn(hci.HCI_StatusAndConnectionHandleReturnParameters):
    packet_sequence_number: int = field(metadata=hci.metadata(2))
    tx_time_stamp: int = field(metadata=hci.metadata(4))
    time_offset: int = field(metadata=hci.metadata(3))

    @classmethod
    def from_parameters(cls, parameters):
        if not parameters or (parameters[0] == 0 and len(parameters) != 12):
            from bumble.core import InvalidPacketError

            raise InvalidPacketError("Invalid ISO TX Sync return width")
        return super().from_parameters(parameters)


@hci.HCI_SyncCommand.sync_command(IsoTxSyncReturn)
@dataclass
class HCI_LE_Read_ISO_TX_Sync_Command(hci.HCI_SyncCommand[IsoTxSyncReturn]):
    # Register the exact wire width locally; no changes to installed Bumble.
    connection_handle: int = field(metadata=hci.metadata(2))


class ClockFeedback:
    def __init__(self, device, *, interval=0.75, failure=lambda error: None):
        self.device, self.interval, self.failure = device, interval, failure
        self.task = self.halt = None
        self.retired = set()

    def start(self, link):
        self.stop()
        host = getattr(self.device, "host", None)
        if host is not None and host.supports_command(hci.HCI_LE_READ_ISO_TX_SYNC_COMMAND):
            self.halt = asyncio.Event()
            self.task = asyncio.create_task(self.poll(link, self.halt))

    async def poll(self, link, halt):
        try:
            while not halt.is_set():
                try:
                    await asyncio.wait_for(halt.wait(), self.interval)
                    return
                except TimeoutError:
                    pass
                command = HCI_LE_Read_ISO_TX_Sync_Command(connection_handle=link.handle)
                host = self.device.host
                # Do not cancel a transmitted command just because an ASE stops:
                # a late Command Complete could otherwise satisfy a NEW command.
                if hasattr(host, "send_sync_command"):
                    response = await host.send_sync_command(command, 2)
                else:
                    response = await self.device.send_sync_command(command)
                # Native USB records its original epoch and QPC time. No Python
                # round-trip/time estimate enters the real-time audio loop.
                if response.status:
                    return
        except asyncio.CancelledError:
            # Cancellation is reserved for whole-receiver teardown.
            raise
        except hci.HCI_Error as error:
            log.debug("ISO clock feedback rejected (%s)", type(error).__name__)
        except Exception as error:
            log.warning("ISO clock feedback channel failed (%s)", type(error).__name__)
            self.failure(error)

    def stop(self):
        task, self.task = self.task, None
        if task is not None:
            self.halt.set()
            self.retired.add(task)
            task.add_done_callback(self.retired.discard)

    async def close(self):
        self.stop()
        if self.retired:
            await asyncio.gather(*tuple(self.retired), return_exceptions=True)

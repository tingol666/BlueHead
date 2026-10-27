"""Cancellation-safe ownership adapter for Bumble periodic advertising syncs.

A PA is registered before its first command. Its pending state is published
before awaiting HCI, so cancellation can always retire the controller request.
The connection may complete while Cancel is in flight; retain the sync handle
and terminate it before removing the host entry. No second HCI/USB owner.
"""

import asyncio
from bumble import hci
from bumble.device import PeriodicAdvertisingSync


class OwnedPeriodicSync(PeriodicAdvertisingSync):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.retirement = asyncio.Lock()
        self.cancel_requested = False

    async def establish(self):
        if self.state != self.State.INIT:
            raise OSError("Periodic sync is not idle")
        self.state = self.State.PENDING
        options = hci.HCI_LE_Periodic_Advertising_Create_Sync_Command.Options(0)
        if self.filter_duplicates:
            options |= options.DUPLICATE_FILTERING_INITIALLY_ENABLED
        await self.device.send_async_command(
            hci.HCI_LE_Periodic_Advertising_Create_Sync_Command(
                options=options,
                advertising_sid=self.sid,
                advertiser_address_type=self.advertiser_address.address_type,
                advertiser_address=self.advertiser_address,
                skip=self.skip,
                sync_timeout=int(self.sync_timeout * 100),
                sync_cte_type=0,
            )
        )

    def on_establishment(
        self, status, sync_handle, advertiser_phy, periodic_advertising_interval, advertiser_clock_accuracy
    ):
        if self.cancel_requested:
            self.status = status
            if status == hci.HCI_SUCCESS:
                # A successful event wins the race with Cancel. terminate()
                # observes this handle after the Cancel response is received.
                self.sync_handle = sync_handle
                self.state = self.State.ESTABLISHED
            else:
                self.state = self.State.CANCELLED
            return
        super().on_establishment(
            status, sync_handle, advertiser_phy, periodic_advertising_interval, advertiser_clock_accuracy
        )

    async def terminate(self):
        async with self.retirement:
            if self.state == self.State.TERMINATED:
                return
            self.cancel_requested = True
            if self.state == self.State.PENDING and self.sync_handle is None:
                try:
                    await self.device.send_sync_command(
                        hci.HCI_LE_Periodic_Advertising_Create_Sync_Cancel_Command()
                    )
                except hci.HCI_Error as error:
                    # A completed establishment makes Cancel disallowed. Its
                    # handle must already have been delivered in HCI order.
                    if error.error_code != 0x0C or self.sync_handle is None:
                        raise
            if self.sync_handle is not None:
                try:
                    await self.device.send_sync_command(
                        hci.HCI_LE_Periodic_Advertising_Terminate_Sync_Command(sync_handle=self.sync_handle)
                    )
                except hci.HCI_Error as error:
                    if error.error_code not in (0x02, 0x42):
                        raise
            self.state = self.State.TERMINATED
            if self in self.device.periodic_advertising_syncs:
                self.device.periodic_advertising_syncs.remove(self)


async def create_owned_sync(device, address, sid, on_created=lambda pa: None):
    # Controller backends may implement their own public PA owner. With Bumble,
    # use this narrow lifecycle adapter rather than patching dependency files.
    update = getattr(device, "_update_periodic_advertising_syncs", None)
    if update is None:
        pa = await device.create_periodic_advertising_sync(address, sid, sync_timeout=3, filter_duplicates=False)
        on_created(pa)
        return pa
    if any(p.advertiser_address == address and p.sid == sid for p in device.periodic_advertising_syncs):
        raise ValueError("This periodic advertising sync already exists")
    pa = OwnedPeriodicSync(device, address, sid, 0, 3, False)
    device.periodic_advertising_syncs.append(pa)
    on_created(pa)
    try:
        await update()
        return pa
    except BaseException:
        cleanup = asyncio.create_task(pa.terminate())
        # Timeout and Stop may both cancel the caller. Controller cleanup has
        # its own native HCI deadline and must outlive either cancellation.
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()
        raise

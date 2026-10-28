"""Standards-based dual-mode announcements; no audio or controller ownership."""

import asyncio
import struct
from bumble import core, hci
from bumble.device import AdvertisingEventProperties, AdvertisingParameters
from bumble.profiles.tmap import Role

HEADSET_APPEARANCE = 0x0942  # Assigned Numbers: Wearable Audio Device / Headset.
LE_AUDIO_CLASS_BIT = 1 << 14
HEADSET_CLASS = 0x240404


def class_of_device(le_audio=False):
    return HEADSET_CLASS | (LE_AUDIO_CLASS_BIT if le_audio else 0)


def announcement(name, *, receive, transmit, broadcast, targeted=False, tmap_roles=0):
    """One complete extended announcement, with the same name/public identity.

    BAP 3.5.3, CAP 8.1 and TMAP 3.4. Do not truncate the name to fit legacy PDUs:
    a legacy connectable ADV_IND is not an LE Audio Unicast Server announcement.
    """
    if not isinstance(name, str) or not 1 <= len(name.encode('utf-8')) <= 200:
        raise ValueError('Invalid LE Audio name')
    contexts = 0x0FFF if receive else 0
    data = [(core.AdvertisingData.FLAGS, b'\x1a'),
            (core.AdvertisingData.APPEARANCE, struct.pack('<H', HEADSET_APPEARANCE)),
            (core.AdvertisingData.COMPLETE_LOCAL_NAME, name.encode('utf-8'))]
    if receive:
        payload = b'\x4e\x18' + bytes([int(targeted)])
        payload += struct.pack('<HHB', contexts, contexts if transmit else 0, 0)
        data.append((core.AdvertisingData.SERVICE_DATA_16_BIT_UUID, payload))
    # VCP/MICP/MCP acceptors and broadcast scan delegators also announce CAP.
    data.append((core.AdvertisingData.SERVICE_DATA_16_BIT_UUID,
                 b'\x53\x18' + bytes([int(targeted)])))
    if broadcast:
        data.append((core.AdvertisingData.SERVICE_DATA_16_BIT_UUID, b'\x4f\x18'))
    if tmap_roles:
        data.append((core.AdvertisingData.SERVICE_DATA_16_BIT_UUID,
                     b'\x55\x18' + struct.pack('<H', int(tmap_roles))))
    return bytes(core.AdvertisingData(data))


def roles(*, receive, transmit, broadcast, le_2m):
    # TMAP UMR also requires BMR; do not falsely publish a role by adding a UUID.
    if not le_2m:
        return Role(0)
    value = Role.CALL_TERMINAL if receive and transmit else Role(0)
    if broadcast:
        value |= Role.BROADCAST_MEDIA_RECEIVER
        if receive:
            value |= Role.UNICAST_MEDIA_RECEIVER
    return value


def parameters():
    return AdvertisingParameters(
        advertising_event_properties=AdvertisingEventProperties(
            is_connectable=True, is_scannable=False, is_legacy=False),
        own_address_type=hci.OwnAddressType.PUBLIC,
        primary_advertising_interval_min=100,
        primary_advertising_interval_max=150,
        # Discovery on 1M remains reachable to scanners before PHY negotiation.
        primary_advertising_phy=hci.Phy.LE_1M,
        secondary_advertising_phy=hci.Phy.LE_1M,
    )


class AudioAdvertisement:
    """One explicitly owned advertising set; restart is controlled by policy."""
    def __init__(self, device):
        self.device, self.current, self.payload = device, None, None
        self.preparing = None
        self.closed = False

    async def update(self, allowed, payload):
        if self.closed:
            return
        if not allowed:
            if self.current is not None and self.current.enabled:
                await self.current.stop()
            return
        if self.current is None:
            if self.preparing is None:
                self.preparing = asyncio.create_task(self.device.create_advertising_set(
                    advertising_parameters=parameters(), advertising_data=payload,
                    auto_start=False, auto_restart=False))
            # Cancellation cannot forget a controller handle created just after
            # the caller timed out. close() must collect and remove that set.
            self.current = await asyncio.shield(self.preparing)
            self.preparing = None
            self.payload = payload
        elif self.payload != payload:
            if self.current.enabled:
                await self.current.stop()
            await self.current.set_advertising_data(payload)
            self.payload = payload
        if not self.current.enabled:
            await self.current.start()

    async def close(self):
        self.closed = True
        if self.preparing is not None:
            pending, self.preparing = self.preparing, None
            try:
                self.current = await asyncio.wait_for(asyncio.shield(pending), 2)
            except BaseException:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                raise
        current, self.current = self.current, None
        if current is not None:
            try:
                if current.enabled:
                    await current.stop()
            finally:
                await current.remove()

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


def announcement(name, *, receive, transmit, broadcast, targeted=False, tmap_roles=0, dual_mode=True):
    """One complete extended announcement, with the same name/public identity.

    BAP 3.5.3, CAP 8.1 and TMAP 3.4. Do not truncate the name to fit legacy PDUs:
    a legacy connectable ADV_IND is not an LE Audio Unicast Server announcement.
    """
    if not isinstance(name, str) or not 1 <= len(name.encode('utf-8')) <= 200:
        raise ValueError('Invalid LE Audio name')
    contexts = 0x0FFF if receive else 0
    data = [(core.AdvertisingData.FLAGS, bytes([0x1a if dual_mode else 0x06])),
            (core.AdvertisingData.APPEARANCE, struct.pack('<H', HEADSET_APPEARANCE)),
            (core.AdvertisingData.COMPLETE_LOCAL_NAME, name.encode('utf-8'))]
    uuids = [0x1853, 0x1844, 0x180a]
    if receive:
        uuids.extend((0x184e, 0x1850))
    if transmit:
        uuids.append(0x184d)
    if broadcast:
        uuids.append(0x184f)
    if tmap_roles:
        uuids.append(0x1855)
    # Incomplete list: GAP/GATT and other services also exist. Service Data
    # remains authoritative; UUID filters used by scanners see the same roles.
    data.append((core.AdvertisingData.INCOMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS,
                 b"".join(struct.pack('<H', value) for value in uuids)))
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


def legacy_announcement(name, *, receive, transmit, broadcast, dual_mode):
    """Supplement the mandatory extended BAP announcement, never replace it.

    Same public identity; a compact discoverable ADV_IND reaches scanners that
    do not surface extended-only devices in their Settings pairing list.
    """
    uuids = [0x1853]
    if receive:
        uuids.extend((0x184e, 0x1850))
    if broadcast:
        uuids.append(0x184f)
    data = [(core.AdvertisingData.FLAGS, bytes([0x1a if dual_mode else 0x06])),
            (core.AdvertisingData.APPEARANCE, struct.pack('<H', HEADSET_APPEARANCE)),
            (core.AdvertisingData.INCOMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS,
             b"".join(struct.pack('<H', value) for value in uuids))]
    if receive:
        data.append((core.AdvertisingData.SERVICE_DATA_16_BIT_UUID,
                     b"\x4e\x18\0" + struct.pack('<HHB', 0x0fff, 0x0fff if transmit else 0, 0)))
    wire = bytes(core.AdvertisingData(data))
    raw = name.encode('utf-8')
    short = raw[:29].decode('utf-8', errors='ignore').encode('utf-8')
    name_type = core.AdvertisingData.COMPLETE_LOCAL_NAME if short == raw else core.AdvertisingData.SHORTENED_LOCAL_NAME
    response = bytes(core.AdvertisingData([(name_type, short)]))
    if not short or len(wire) > 31 or len(response) > 31:
        raise ValueError('Invalid legacy discovery payload')
    return wire, response


def parameters(*, legacy=False):
    return AdvertisingParameters(
        advertising_event_properties=AdvertisingEventProperties(
            is_connectable=True, is_scannable=legacy, is_legacy=legacy),
        own_address_type=hci.OwnAddressType.PUBLIC,
        primary_advertising_interval_min=100,
        primary_advertising_interval_max=150,
        # Discovery on 1M remains reachable to scanners before PHY negotiation.
        primary_advertising_phy=hci.Phy.LE_1M,
        secondary_advertising_phy=hci.Phy.LE_1M,
    )


class AudioAdvertisement:
    """One explicitly owned advertising set; restart is controlled by policy."""
    def __init__(self, device, *, legacy=False):
        self.legacy, self.scan_response = legacy, b""
        self.device, self.current, self.payload = device, None, None
        self.preparing = None
        self.closed = False

    async def update(self, allowed, payload, scan_response=b""):
        if self.closed:
            return
        if not allowed:
            if self.current is not None and self.current.enabled:
                await self.current.stop()
            return
        if self.current is None:
            if self.preparing is None:
                options = dict(advertising_parameters=parameters(legacy=self.legacy), advertising_data=payload,
                               auto_start=False, auto_restart=False)
                if self.legacy:
                    options['scan_response_data'] = scan_response
                self.preparing = asyncio.create_task(self.device.create_advertising_set(**options))
            # Cancellation cannot forget a controller handle created just after
            # the caller timed out. close() must collect and remove that set.
            pending = self.preparing
            try:
                self.current = await asyncio.shield(pending)
            except asyncio.CancelledError:
                raise
            except BaseException:
                # A completed failed create cannot later produce a handle.
                # Keep cancelled/in-flight creation for close() to collect.
                if pending.done():
                    self.preparing = None
                raise
            self.preparing = None
            self.payload, self.scan_response = payload, scan_response
        elif self.payload != payload or self.scan_response != scan_response:
            if self.current.enabled:
                await self.current.stop()
            await self.current.set_advertising_data(payload)
            if self.legacy and self.scan_response != scan_response:
                await self.current.set_scan_response_data(scan_response)
            self.payload, self.scan_response = payload, scan_response
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

"""Truthful Classic audio discovery records; no borrowed manufacturer identity."""
import zlib

from bumble import core, hci, sdp


def inquiry_response(name, calls=True, tx_power=None):
    services = [0x110B, 0x110C, 0x110E, 0x110F]
    if calls:
        services.append(0x111E)
    data = [(core.AdvertisingData.COMPLETE_LOCAL_NAME, name.encode("utf-8")),
            (core.AdvertisingData.COMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS,
             b"".join(service.to_bytes(2, "little") for service in services))]
    if tx_power is not None and -127 <= tx_power <= 20:
        data.append((core.AdvertisingData.TX_POWER_LEVEL, int(tx_power).to_bytes(1, "little", signed=True)))
    return bytes(core.AdvertisingData(data))


def add_server_record(device):
    # SDP Server record 0, SDP 1.0. Hash reflects the actual static service set.
    # RFCOMM channel/transport parameters may change on restart without a change
    # to the service database's public feature set (Apple accessory guidance).
    transient = {sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
                 sdp.SDP_ADDITIONAL_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID}
    data = b"".join(handle.to_bytes(4, "big") + b"".join(
        attribute.id.to_bytes(2, "big") + bytes(attribute.value) for attribute in attributes if attribute.id not in transient)
        for handle, attributes in sorted(device.sdp_service_records.items()) if handle != 0)
    element, attribute = sdp.DataElement, sdp.ServiceAttribute
    device.sdp_service_records[0] = [
        attribute(sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID, element.unsigned_integer_32(0)),
        attribute(sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
                  element.sequence([element.uuid(core.UUID.from_16_bits(0x1000))])),
        attribute(0x0200, element.sequence([element.unsigned_integer_16(0x0100)])),
        attribute(0x0201, element.unsigned_integer_32(zlib.crc32(data))),
    ]


async def update_inquiry_power(device, name, calls):
    # Omit unavailable/calibration-unknown power, rather than inventing a value.
    power = None
    if device.host.supports_command(hci.HCI_READ_INQUIRY_RESPONSE_TRANSMIT_POWER_LEVEL_COMMAND):
        try:
            response = await device.send_sync_command(hci.HCI_Read_Inquiry_Response_Transmit_Power_Level_Command())
            if response.status == 0:
                power = response.tx_power
        except Exception:
            pass
    device.inquiry_response = inquiry_response(name, calls, power)

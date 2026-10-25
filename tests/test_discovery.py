from types import SimpleNamespace

from bumble import core, sdp

from bluehead.backend import Receiver, Settings
from bluehead.discovery import inquiry_response, add_server_record, update_inquiry_power
from .test_protocols import Network


def test_inquiry_advertises_actual_audio_roles_and_measured_power_only():
    data = core.AdvertisingData.from_bytes(inquiry_response("BlueHead", True, -7))
    assert data.get(core.AdvertisingData.COMPLETE_LOCAL_NAME) == "BlueHead"
    services = data.get(core.AdvertisingData.COMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS)
    assert {item.to_hex_str() for item in services} == {"110B", "110C", "110E", "110F", "111E"}
    assert int.from_bytes(data.get(core.AdvertisingData.TX_POWER_LEVEL, raw=True), "little", signed=True) == -7
    assert data.get(core.AdvertisingData.DEVICE_ID) is None
    no_power = core.AdvertisingData.from_bytes(inquiry_response("BlueHead", False))
    assert no_power.get(core.AdvertisingData.TX_POWER_LEVEL) is None


async def test_controller_power_query_failure_does_not_break_discovery():
    async def unsupported(_):
        raise RuntimeError("unsupported")
    device = SimpleNamespace(host=SimpleNamespace(supports_command=lambda _: True), send_sync_command=unsupported)
    await update_inquiry_power(device, "BlueHead", True)
    assert device.inquiry_response == inquiry_response("BlueHead", True)


async def test_sdp_server_database_state_tracks_supported_services(tmp_path):
    receiver = Receiver(Settings(calls=False), tmp_path, lambda *a, **kw: None)
    device = Network(1).devices[0]
    receiver.attach(device)
    try:
        record = device.sdp_service_records[0]
        state = sdp.ServiceAttribute.find_attribute_in_list(record, 0x0201).value
        add_server_record(device)
        assert sdp.ServiceAttribute.find_attribute_in_list(device.sdp_service_records[0], 0x0201).value == state
        device.sdp_service_records.pop(0x10020)
        add_server_record(device)
        assert sdp.ServiceAttribute.find_attribute_in_list(device.sdp_service_records[0], 0x0201).value != state
    finally:
        await receiver.avrcp.aclose()

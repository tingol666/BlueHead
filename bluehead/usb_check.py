"""Explicit local USB voice check, shared by CLI and the frozen executable."""
import asyncio
import sys
import traceback


async def probe(spec):
    from bumble.device import Device, DeviceConfiguration
    import usb1
    from .adapter import AdapterLease
    from .usb_transport import open_transport

    result = {"passed": False, "packaged": bool(getattr(sys, "frozen", False)), "scope": "physical USB ISO callbacks and alternate-setting changes; no radio links or local audio"}
    lease = AdapterLease(spec)
    lease.acquire()
    try:
        async with await open_transport(spec) as transport:
            config = DeviceConfiguration()
            config.classic_enabled, config.le_enabled = True, False
            device = Device.from_config_with_hci(config, transport.source, transport.sink)
            await asyncio.wait_for(device.power_on(), 10)
            version = usb1.getVersion()
            result["libusb"] = f"{version.major}.{version.minor}.{version.micro}"
            result["stages"] = []
            for alternate in (6, 2, 6):
                transport.set_sco_alternate_setting(alternate)
                # Nonexistent radio handle. Exercise the USB OUT endpoint with
                # silence; the controller discards it instead of calling a peer.
                device.host.send_sco_sdu(0x101, bytes(transport.usb_sink.sco_packet_data_size))
                await asyncio.sleep(.25)
                result["stages"].append({"alternate": alternate,
                                        "callbacks": transport.usb_source.iso_callbacks,
                                        "metadata_errors": transport.usb_source.iso_errors})
            transport.set_sco_alternate_setting(0)
            await asyncio.wait_for(device.power_off(), 2)
            assert transport.usb_source.iso_errors == 0
        result["clean_shutdown"] = True
        result["passed"] = True
    except Exception:
        result["error"] = traceback.format_exc()
    finally:
        lease.close()
    return result


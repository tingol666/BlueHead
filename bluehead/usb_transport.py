"""Use the auditable BlueHead USB transport with Bumble's other transports."""
from bumble.transport import open_transport as upstream_open_transport

from .windows_usb import load_libusb, open_usb_transport


async def open_transport(spec):
    if spec.startswith('usb:'):
        from .native_runtime import current, NativeTransport
        client = current()
        if client is not None:
            return NativeTransport(client, spec)
        return await open_usb_transport(spec[4:])
    return await upstream_open_transport(spec)


__all__ = ['load_libusb', 'open_transport']

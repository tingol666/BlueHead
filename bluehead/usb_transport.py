"""Use the auditable BlueHead USB transport with Bumble's other transports."""
from bumble.transport import open_transport as upstream_open_transport

from .windows_usb import load_libusb, open_usb_transport


async def open_transport(spec):
    if spec.startswith(('usb:', 'usb-id:')):
        from .native_control import current, NativeTransport
        client = current()
        if client is not None:
            return NativeTransport(client, spec)
        if spec.startswith('usb-id:'):
            raise OSError('设备身份接收需要完整的原生后台，请从 BlueHead 桌面启动')
        return await open_usb_transport(spec[4:])
    return await upstream_open_transport(spec)


__all__ = ['load_libusb', 'open_transport']

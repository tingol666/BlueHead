"""Real libusb transfer layouts; never submit a transfer to physical hardware."""
import asyncio
import contextlib
from types import SimpleNamespace
import weakref

import numpy as np
import pytest
from bumble import hci, hfp
import usb1

from bluehead.audio import Mixer
from bluehead.calls import CallManager
from .voice_reference import Msbc
from bluehead import windows_usb as usb
from .test_protocols import Network


@pytest.fixture
def transfer():
    usb.load_libusb()
    item = usb1.USBTransfer(
        context=None, handle=None, iso_packets=usb.MAX_SCO_IN_PACKETS,
        before_submit=lambda transfer: None, after_completion=lambda transfer: None,
        getFinalizer=lambda obj, callback, **kw: weakref.finalize(obj, callback, **kw),
        short_is_error=False, add_zero_packet=False,
    )
    item.setIsochronous(0x83, usb.MAX_SCO_IN_PACKETS * 63, user_data=hci.HCI_SYNCHRONOUS_DATA_PACKET)
    try:
        yield item
    finally:
        item.close()


def descriptors(transfer):
    return usb1.libusb1.get_iso_packet_list(transfer._USBTransfer__transfer)


def test_iso_uses_owned_buffer_even_when_native_buffer_pointer_is_invalid(transfer):
    original = transfer._USBTransfer__transfer.contents.buffer
    try:
        transfer.getBuffer()[:4] = b"safe"
        descriptors(transfer)[0].actual_length = 4
        # The old iterISO() would copy from address 1 and abort CPython.
        transfer._USBTransfer__transfer.contents.buffer = 1
        assert list(usb.iso_packets(transfer))[0] == (0, b"safe")
    finally:
        transfer._USBTransfer__transfer.contents.buffer = original


def test_invalid_iso_lengths_are_rejected_before_copy(transfer):
    descriptor = descriptors(transfer)[0]
    previous = descriptor.actual_length
    try:
        descriptor.actual_length = 0xffffffff
        with pytest.raises(ValueError, match="USB ISO"):
            list(usb.iso_packets(transfer))
    finally:
        descriptor.actual_length = previous


@pytest.mark.parametrize("stride", [0, 0xffffffff])
def test_returned_native_strides_do_not_control_owned_buffer_offsets(transfer, stride):
    descriptors(transfer)[0].length = stride
    descriptors(transfer)[1].actual_length = 4
    transfer.getBuffer()[63:67] = b"next"
    assert list(usb.iso_packets(transfer))[1] == (0, b"next")


async def test_actual_usb_callback_delivers_voice_through_host_to_call_manager(transfer, monkeypatch):
    import bluehead.calls as calls
    def disabled(*args):
        raise RuntimeError("no physical microphone in USB test")
    monkeypatch.setattr(calls, "Microphone", disabled)
    network = Network(1)
    await network.start()
    device = network.devices[0]
    mixer = Mixer(buffer_ms=0)
    manager = CallManager(device, mixer, lambda *args, **kwargs: None)
    sent = []
    device.host.send_sco_sdu = lambda handle, data: sent.append(data)
    link = SimpleNamespace(handle=71, air_mode=hci.CodecID.TRANSPARENT, once=lambda *args: None)
    device.sco_links[71] = link
    manager.on_link("phone", SimpleNamespace(active_codec=hfp.AudioCodec.MSBC), link)
    encoded = Msbc().encode(np.full(120, 6000, dtype="<i2").tobytes())
    packet = hci.HCI_SynchronousDataPacket(connection_handle=71, packet_status=0,
                                          data_total_length=60, data=encoded)
    payload = bytes(packet)[1:]
    assert len(payload) == 63
    transfer.getBuffer()[:63] = payload
    descriptors(transfer)[0].actual_length = 63
    resubmitted = []
    monkeypatch.setattr(transfer, "submit", lambda: resubmitted.append(True))
    endpoint = SimpleNamespace(getAddress=lambda: 0x83)
    source = usb.UsbPacketSource(None, {}, None, None, endpoint)
    source.isochronous_in_transfers = [transfer]
    source.done[transfer] = asyncio.Event()
    source.sink = device.host
    source.dequeue_task = asyncio.create_task(source.dequeue())
    try:
        source.transfer_callback(transfer)
        async with asyncio.timeout(2):
            while manager.statistics["phone"]["decoded_frames"] == 0:
                await asyncio.sleep(.005)
        assert resubmitted == [True] and len(sent) == 2
        assert manager.statistics["phone"]["rx_packets"] == 1
        assert mixer.inputs["call:phone"].frames > 0 and source.iso_errors == 0
        # A completion racing close must signal completion and never resubmit.
        source.closed = True
        source.transfer_callback(transfer)
        await asyncio.sleep(0)
        assert source.done[transfer].is_set() and resubmitted == [True]
    finally:
        source.close()
        source.dequeue_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await source.dequeue_task
        manager.close()
        await manager.wait_closed()
        await network.close()


async def test_bad_metadata_drops_packet_and_keeps_receive_transfer_running(transfer, monkeypatch):
    endpoint = SimpleNamespace(getAddress=lambda: 0x83)
    source = usb.UsbPacketSource(None, {}, None, None, endpoint)
    source.isochronous_in_transfers = [transfer]
    source.done[transfer] = asyncio.Event()
    resubmitted = []
    monkeypatch.setattr(transfer, "submit", lambda: resubmitted.append(True))
    descriptors(transfer)[0].actual_length = 0xffffffff
    try:
        source.transfer_callback(transfer)
        assert source.iso_errors == 1 and resubmitted == [True] and source.queue.empty()
    finally:
        descriptors(transfer)[0].actual_length = 0
        source.close()


async def test_completion_racing_cancel_cannot_resubmit_retired_transfer(transfer):
    endpoint = SimpleNamespace(getAddress=lambda: 0x83)
    source = usb.UsbPacketSource(None, {}, None, None, endpoint)
    source.isochronous_in_transfers = [transfer]
    source.done[transfer] = asyncio.Event()
    submitted, resubmitted = [True], []
    def completed_during_cancel():
        submitted[0] = False
        source.transfer_callback(transfer)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(transfer, "isSubmitted", lambda: submitted[0])
        patch.setattr(transfer, "cancel", completed_during_cancel)
        patch.setattr(transfer, "submit", lambda: resubmitted.append(True))
        source.stop_sco()
        await asyncio.sleep(0)
        assert not resubmitted and source.done[transfer].is_set()
        assert transfer in source.retired_sco_transfers
    source.close()

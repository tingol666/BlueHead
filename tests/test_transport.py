"""Voice must reach the USB isochronous interface, not only HFP signalling."""
import pytest

from bluehead import backend


@pytest.mark.parametrize("calls, expected", [
    (True, "usb:0000:0000#0+sco=0"),
    (False, "usb:0000:0000#0"),
])
async def test_receiver_requests_voice_transport_when_calls_enabled(tmp_path, monkeypatch, calls, expected):
    opened = []

    async def observe_transport(spec):
        opened.append(spec)
        raise ConnectionError("test ends before touching physical USB")

    monkeypatch.setattr(backend, "open_transport", observe_transport)
    # A signalling test must not contend with a real receiver's adapter lease.
    receiver = backend.Receiver(
        backend.Settings(transport="usb:0000:0000#0", calls=calls), tmp_path,
        lambda *args, **kwargs: None,
    )
    with pytest.raises(ConnectionError, match="test ends"):
        await receiver.run()
    assert opened == [expected]

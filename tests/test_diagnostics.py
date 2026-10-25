import logging
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from bluehead.adapter import AdapterLease
from bluehead.diagnostics import MetadataOnly


@pytest.mark.parametrize("logger,message,expected", [
    ("bumble.hfp", ">>> AT+BCS=2", True),
    ("bumble.hfp", "<<< +CIEV: 2,1", True),
    ("bumble.hfp", "<<< +CLIP: \"123456789\"", False),
    ("bumble.hfp", ">>> ATD123456789;", False),
    ("bumble.host", "<<< HCI_SYNCHRONOUS_CONNECTION_COMPLETE_EVENT:\n air_mode: 3", True),
    ("bumble.host", "<<< HCI_SYNCHRONOUS_DATA_PACKET:\n audio hex payload", False),
    ("bumble.host", "<<< HCI_LINK_KEY_NOTIFICATION_EVENT:\n key: secret", False),
    ("bumble.host", ">>> HCI_LINK_KEY_REQUEST_REPLY_COMMAND:\n key: secret", False),
    ("bumble.transport.usb", "### SCO packet: 63 audio hex payload", False),
    ("bumble.transport.usb", "switching SCO alternate setting to 6", True),
    ("bluehead.calls", "SCO counters: rx_packets=42 tx_packets=42", True),
])
def test_protocol_diagnostics_exclude_audio_keys_and_phone_numbers(logger, message, expected):
    record = logging.LogRecord(logger, logging.DEBUG, "", 0, message, (), None)
    assert bool(MetadataOnly().filter(record)) == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows receiver instance protection")
def test_adapter_lease_is_enforced_across_processes_and_released():
    moniker = "test:" + uuid.uuid4().hex
    lease = AdapterLease(moniker + "+sco=0")
    child = """
import sys
from bluehead.adapter import AdapterLease
lease = AdapterLease(sys.argv[1])
try:
    lease.acquire()
except RuntimeError:
    raise SystemExit(3)
else:
    lease.close()
"""
    def attempt():
        return subprocess.run([sys.executable, "-c", child, moniker],
                              cwd=Path(__file__).resolve().parents[1], timeout=5,
                              capture_output=True, text=True).returncode
    lease.acquire()
    try:
        assert attempt() == 3
    finally:
        lease.close()
    assert attempt() == 0

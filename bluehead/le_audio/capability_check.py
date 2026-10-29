"""Intersect native transport evidence with the powered-on signalling host.

Features alone do not promise that a controller implements every required HCI
procedure. Check each role independently before publishing its GATT/AD identity;
never discover an absent command by failing in the middle of an audio stream.
"""

from dataclasses import replace
from bumble import hci


ADVERTISING = (
    hci.HCI_LE_SET_EXTENDED_ADVERTISING_PARAMETERS_COMMAND,
    hci.HCI_LE_SET_EXTENDED_ADVERTISING_DATA_COMMAND,
    hci.HCI_LE_SET_EXTENDED_ADVERTISING_ENABLE_COMMAND,
    hci.HCI_LE_REMOVE_ADVERTISING_SET_COMMAND,
)
DATA_PATH = (hci.HCI_LE_SETUP_ISO_DATA_PATH_COMMAND, hci.HCI_LE_REMOVE_ISO_DATA_PATH_COMMAND)
UNICAST = (
    hci.HCI_LE_SET_HOST_FEATURE_COMMAND,
    hci.HCI_LE_ACCEPT_CIS_REQUEST_COMMAND,
    hci.HCI_LE_REJECT_CIS_REQUEST_COMMAND,
    hci.HCI_DISCONNECT_COMMAND,
)
BROADCAST = (
    hci.HCI_LE_SET_EXTENDED_SCAN_PARAMETERS_COMMAND,
    hci.HCI_LE_SET_EXTENDED_SCAN_ENABLE_COMMAND,
    hci.HCI_LE_PERIODIC_ADVERTISING_CREATE_SYNC_COMMAND,
    hci.HCI_LE_PERIODIC_ADVERTISING_CREATE_SYNC_CANCEL_COMMAND,
    hci.HCI_LE_PERIODIC_ADVERTISING_TERMINATE_SYNC_COMMAND,
    hci.HCI_LE_BIG_CREATE_SYNC_COMMAND,
    hci.HCI_LE_BIG_TERMINATE_SYNC_COMMAND,
)


def checked_support(support, device):
    """Return usable roles and diagnostic causes, without opening any transport.

    Bumble's CIS/BIS lifecycle also needs its ISO queue initialized from Read
    Buffer Size v2. It is not a second audio sender: native code still owns all
    ISO/PCM. A missing host queue must be caught before remote pairing succeeds.
    """
    host = device.host
    issues = []

    def commands(values):
        absent = tuple(hci.HCI_Command.command_name(op) for op in values if not host.supports_command(op))
        if absent:
            issues.extend(absent)
        return not absent

    advertising = device.supports_le_features(hci.LeFeatureMask.LE_EXTENDED_ADVERTISING)
    sets = getattr(host, 'number_of_supported_advertising_sets', 0)
    if not advertising or type(sets) is not int or sets < 1:
        issues.append('extended_advertising_unavailable')
        return replace(support, receive=False, transmit=False, broadcast=False), tuple(issues)
    if not commands(ADVERTISING + DATA_PATH):
        return replace(support, receive=False, transmit=False, broadcast=False), tuple(issues)
    queue = host.iso_packet_queue
    if queue is None or queue.max_packet_size < 5 or queue.max_in_flight < 1:
        issues.append('host_iso_buffers_unavailable')
        return replace(support, receive=False, transmit=False, broadcast=False), tuple(issues)

    receive = support.receive
    if receive and (not device.supports_le_features(hci.LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM_PERIPHERAL)
                    or not commands(UNICAST)):
        receive = False
        issues.append('unicast_unavailable')
    broadcast = support.broadcast
    if broadcast and (not device.supports_le_features(hci.LeFeatureMask.SYNCHRONIZED_RECEIVER |
                                                     hci.LeFeatureMask.LE_PERIODIC_ADVERTISING)
                      or not commands(BROADCAST)):
        broadcast = False
        issues.append('broadcast_unavailable')
    return replace(support, receive=receive, transmit=support.transmit and receive,
                   broadcast=broadcast), tuple(issues)

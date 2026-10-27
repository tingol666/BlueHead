# Controller compatibility

BlueHead is a general Windows application. The current adapter, audio endpoints,
installation path and firmware must be discovered at runtime. No workstation
configuration belongs in defaults, examples, release notes or compatibility rules.

## Physical identity

The native owner uses the pinned libusb Windows backend's session-data DEVINST
to resolve the Windows physical USB instance. Composite functions resolve to
their USB parent. The same canonical instance is hashed by the platform helper
and native runtime into an opaque selector, `usb-id:<24 lowercase hex digits>`.
Selectors are private device preferences, not public telemetry identifiers.

An explicit selector never falls back to the first adapter. Legacy VID/PID/index
settings migrate only when one matching device exists. Identical models require
an explicit choice; enumeration order is not physical identity. Hardware without
a unique serial number can change identity when moved to another USB port.

The native owner holds a physical-controller lease before opening/claiming its
USB interfaces. Legacy ordinal leases remain during migration so older receiver
processes cannot silently acquire a second owner. Driver mutation checks both.
Descriptor enumeration never claims interfaces or loads firmware.

## Initialization boundary

Vendor bootstrap implementations borrow write/event-pump/progress ports from
the existing owner. They cannot enumerate, reopen or independently own USB.
Bootstrap command accounting finishes before the operational host receives HCI.
The recovery helper passes an explicit physical selector to preparation; there
is no fixed product target. Unsupported native preparation does not block
restoring the original Windows driver.

Transport capabilities report their evidence scope: descriptor support is not
an actual peer call test. Media/voice transport availability, firmware state and
verified peer behavior are separate facts.

## Original driver selection

An adopted adapter's original driver is selected from its Windows-compatible
Bluetooth-class candidates using Windows rank, date and version. Brand names
and localized friendly descriptions do not decide compatibility. Failed/unknown
rank and explicitly bad candidates are excluded. Exported packages select the
original INF by content, never the first filename. Backup metadata without the
actual backup file is not presented as a complete recovery backup.

## Verification stages

1. Physical identity, native ownership, platform selection and backend contracts.
2. Firmware catalog, verified acquisition, package extraction and caching.
3. Vendor initialization, original-driver handoff and bounded recovery.
4. Shared UI policy, capability degradation, compatibility matrix and release.

Only completed stages are counted in the version's validation record. Synthetic
USB/HCI tests run production ownership code but do not establish every hardware
revision's real-device compatibility.

References: [libusb session data](https://libusb.sourceforge.io/api-1.0/group__libusb__dev.html),
[Windows device location](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/devpkey-device-locationpaths),
[driver rank](https://learn.microsoft.com/en-us/windows/win32/api/setupapi/ns-setupapi-sp_drvinstall_params).

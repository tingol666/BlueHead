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

## Firmware trust and acquisition

The application verifies the shipped catalog's P-256 signature with Windows CNG
before parsing or selecting resources. Profiles require chip facts and, for
board configurations, an explicit USB board match. Each image/config/license
has a fixed length and SHA-256. Equal-specificity matches fail closed. Firmware
formats and protocols are compiled backends, never downloaded executable code.

The content-addressed per-user cache is bounded and reverified on use. Minimal
bundled resources work offline; optional profiles can acquire files from two
signed HTTPS source entries pinned to an upstream revision. Redirect hosts,
response size and time are restricted. Downloads run in a separate process
that cannot also be an audio/HCI host. Atomic commits prevent partial files
from becoming valid resources. License and immutable origin metadata accompany
the resources; the current release is the catalog update boundary.

Original packages can provide matching bytes through bounded static ZIP, CAB,
MSI stream and embedded-CAB extraction. Directory and file links, traversal,
duplicate payloads and oversized data are rejected. No installer, custom action
or imported DLL executes. Package extraction does not authenticate arbitrary
firmware: the native verifier still requires the signed catalog's exact digest.
Unknown payload revisions or wrappers use the original Windows driver path.
Private catalog signing keys are external maintainer material and are excluded
from source archives and releases.

## Vendor initialization and handoff

| Controller state | Initialization path | Evidence |
| --- | --- | --- |
| Intel secure TLV variants 0x17–0x19 in the compiled USB bootstrap registry | Signed catalog image/config pair, secure RAM download, operational version, standard HCI probe | Production-owner synthetic cold/warm/failure tests |
| Realtek v1 epatch profiles: 8761BU, 8821C, 8822B, 8723D; explicit catalog board IDs only | Chip/board/ROM/project matching, complete range validation, fragment acknowledgement, exact loaded version | Four immutable image/config parser vectors; native download model |
| Already operational Intel, Realtek, Broadcom, Qualcomm, MediaTek and other classic USB HCI controllers | Standard reset/version/features/buffers/address probe, without guessed vendor writes | Standard HCI model; each physical revision still needs its own validation |
| Missing resource | Independent verified acquisition/import, then explicit retry | CNG/content-store and static-package checks |
| Unknown image format, board or native initialization failure | One original-driver initialization handoff, then native readiness recheck | Platform transaction port checks |
| Handoff loses RAM firmware, needs reboot or remains unusable | Return to Windows; explain restart or Windows shared-media option | Bounded state-machine checks |

The native vendor path only borrows its owner's command and event ports. It
never opens another USB context, enables a second HCI ledger or changes audio
processing. All capabilities require successful standard HCI replies; a driver
name alone cannot mean ready. Voice also requires synchronous USB endpoints and
controller SCO/eSCO features. A media-only session reports the limitation and
does not advertise voice services; the saved voice and quality choices survive.

Original-driver handoff waits for Windows radio readiness, releases that owner,
binds dedicated reception and rechecks the same physical controller. A rebind
may erase volatile firmware, so the recheck is mandatory. There is one handoff,
not a retry loop. Each mutation phase is persisted in the protected recovery
record; failure restores the original driver or reports recovery/restart needs.

Protocol references: [Bumble Realtek](https://google.github.io/bumble/drivers/realtek.html),
[Linux Bluetooth initialization](https://github.com/torvalds/linux/tree/v6.17/drivers/bluetooth).
The native parser is Apache-2.0; vendor firmware keeps its own redistribution
terms. The Linux implementation is used to cross-check wire protocol facts,
including fragment wrap and the terminating fragment, not linked into BlueHead.

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

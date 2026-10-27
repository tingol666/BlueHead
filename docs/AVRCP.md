# AVRCP control and Apple standard audio

BlueHead 2.0 separates the Bluetooth control plane from the native PCM engine.
`bluehead.avrcp` uses Bumble's AV/C frames, AVRCP command models and SDP record
builders. It owns the small amount of application state that must be isolated
for every BR/EDR ACL connection. It neither captures audio nor performs DSP.

## Supported surface

| Role | Implemented behavior |
| --- | --- |
| Controller, category 1 | Play, pause, stop, previous, next, including press and release |
| Controller metadata | Current title, artist, album and duration; bounded Unicode decoding |
| Controller notifications | Playback state and track changes; position, now-playing and addressed-player changes when the peer advertises them |
| Target, category 2 | UNIT INFO, SUBUNIT INFO, volume up/down, mute, absolute volume and volume-change notifications |
| Transport | One AVCTP control PSM listener, separate peer sessions, incoming/outgoing connections, collision recovery and bounded fragmentation |

SDP advertises category 1 for CT and category 2 for TG. It does not advertise
browsing, cover art, player application settings, group navigation or a remote
media library. No private Apple accessory identity or private command is used.

## Integration

All calls and callbacks run on the Bluetooth asyncio loop. Callbacks must be
synchronous and fast; marshal UI work to its own thread rather than blocking HCI.

```python
from bluehead.avrcp import AvrcpManager

controls = AvrcpManager(
    emit=emit_state,                  # emit_state("avrcp", key=..., **snapshot)
    get_volume=get_standard_volume,   # key -> float in 0..1
    on_volume=apply_standard_volume,  # (key, float in 0..1), remote changes only
)
controls.attach(device)               # after existing A2DP/HFP SDP setup

await controls.command(peer_key, "play_pause")
await controls.command(peer_key, "next")
controls.set_volume(peer_key, 0.5)    # local change; notifies a subscribed peer
state = controls.snapshot(peer_key)
await controls.aclose()              # before closing the HCI transport
```

`attach` listens for classic ACL connections automatically. Explicit
`on_connection(connection)` calls are also idempotent. `await connect(connection)`
opens control on demand; `await disconnect(key)` releases only that peer's control
channel. A new ACL creates a fresh session. `auto_connect=False` is useful for
tests and applications that open control on demand.

The snapshot contains `connected`, `controls`, `playback_status`, `title`,
`artist`, `album`, `duration_ms`, `position_ms` and `volume`. Missing duration or
position is `None`; missing metadata is an empty string. `playback_status` is
`playing`, `paused`, `stopped`, `forward_seek`, `reverse_seek`, `error` or `unknown`.
Playback state is obtained from the peer, never inferred by counting UI clicks.

Metadata is personal listening information. Snapshots are ephemeral and the
control implementation never logs raw packets, song titles or artists. Do not
automatically include these fields in persistent logs or exported diagnostics.

## Volume ownership

AVRCP's standard range is 0..127, representing 0..100% rendering volume. The
callback exposes this as 0..1. Keep the user's 100..200% software amplification
in a separate gain stage. Do not compress 0..200% into the standard range or
multiply the same synchronized volume twice.

An incoming SetAbsoluteVolume receives an ACCEPTED response containing the
effective volume. It does **not** complete VOLUME_CHANGED. A real local change or
relative-volume key completes that notification once; the peer must re-register.
`on_volume` is only called for remote changes, preventing UI feedback loops.

## Lifetime and bounds

- Each peer owns its 16 transaction labels, tasks, subscriptions and metadata.
- Ordinary requests have a configurable timeout (default two seconds). The final
  CHANGED notification may wait indefinitely after a valid INTERIM response.
- Disconnect cancels outstanding transactions and clears metadata. A stale
  channel callback cannot update a replacement connection.
- Successful requests recycle labels. Abandoned labels remain quarantined until
  the control channel changes, because AVCTP has no sequence number beyond its
  four-bit label. Exhaustion recovers only AVCTP; it does not disconnect A2DP/HFP.
- Metadata uses RequestContinuingResponse; timeout uses AbortContinuingResponse.
  A response is limited to 65,535 parameter bytes and 256 AVRCP fragments.
- Display strings are limited to 1,024 characters. Attribute lengths are checked;
  UTF-8 and common ASCII/Latin-1/UTF-16 encodings are handled. Unknown encodings
  are skipped and malformed Unicode is replaced safely.
- Simultaneous control connections close both colliding channels and retry after
  randomized 100..1000 ms delays. Shutdown and channel closure are bounded.

## Verification and compatibility claims

The maintainer regression suite exchanges actual BR/EDR,
SDP, L2CAP, AV/C and AVRCP packets through Bumble virtual controllers. They cover
both roles, two peers, long metadata continuation, notification re-registration,
volume feedback rules, late replies, cancellation, malformed input, collisions
and reconnects. They do not prove radio or Apple-device interoperability.

Apple's public profile list includes HFP on supported iPads as well as iPhones.
Recording and video apps choose their own input route: HFP availability does not
mean every app will use a Bluetooth microphone. AAC/SBC media, actual call and
recording routes, Siri interruptions, volume synchronization, media recovery and
Windows device switching still require real iPhone/iPad acceptance tests.
Standard AVRCP compatibility does not imply AirPods-only capabilities, browsing,
Apple certification or Apple LC3-SWB support.

## Primary references

- [Bumble upstream AVRCP](https://github.com/google/bumble/blob/main/bumble/avrcp.py)
- [Bumble upstream AV/C](https://github.com/google/bumble/blob/main/bumble/avc.py)
- [AVRCP 1.6.3](https://files.bluetooth.com/wp-content/uploads/dlm_uploads/2024/10/AVRCP_v1.6.3.pdf): tables 3.1/4.1/4.5, sections 4.1.1, 6.7, 6.8 and 6.13
- [AVCTP 1.4](https://www.bluetooth.org/DocMan/handlers/DownloadDoc.ashx?doc_id=260858): sections 4 and 6
- [Apple Accessory Design Guidelines](https://developer.apple.com/accessories/Accessory-Design-Guidelines.pdf), 2026-09-21 edition: sections 53 and 57.11.5
- [Apple profile compatibility](https://support.apple.com/en-us/102842)
- [AVAudioSession allowBluetoothHFP](https://developer.apple.com/documentation/avfaudio/avaudiosession/categoryoptions-swift.struct/allowbluetoothhfp)

Maintenance regression fixtures and stress harnesses mentioned here are not part of the public build inputs. For the built-in product diagnostics available from a clean checkout, see [DIAGNOSTICS](DIAGNOSTICS.md).

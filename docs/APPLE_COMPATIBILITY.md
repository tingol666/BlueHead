# iPhone and iPad compatibility

BlueHead implements standard Bluetooth Classic audio profiles. Version 2.0.0
has offline protocol and native-audio regression coverage; it has not
yet completed real iPhone/iPad acceptance testing. This page distinguishes an
implemented path from a verified device/app combination.

## Implemented standard paths

| Path | BlueHead behavior | Verification still needed |
| --- | --- | --- |
| A2DP AAC-LC | MPEG-2/4 AAC-LC capabilities, LATM decoding, stereo 44.1/48 kHz, VBR handling | Actual AAC selection, changing content/bitrate, radio stability |
| A2DP SBC | Standard fallback with 44.1/48 kHz and bitpool up to 53 | Actual SBC selection and media quality |
| AVRCP controller | Play/pause/stop/previous/next; current title/artist/album/duration; state and track notifications | iOS/iPadOS media apps, lock screen, app switching |
| AVRCP volume target | Standard absolute and relative rendering volume, notification re-registration | Device volume keys, phone/tablet sliders and reconnect behavior |
| HFP audio | Standard microphone and output path with negotiated codec fallback | Supported calls, FaceTime/VoIP, Siri and recording apps |
| Interruptions | Session generations discard stale PCM; media restart and native SRC state are reset | Real call/Siri interruption and media recovery |

AVRCP standard volume is separate from BlueHead's 0..200% local gain. A remote
standard-volume update does not rewrite a saved 175% gain or change another
device's gain. AVRCP volume concerns A2DP rendering; HFP has its own volume
procedure and voice route.

BlueHead does not advertise AVRCP browsing or cover art. A player may provide
partial or no metadata; the UI then shows no title or an unknown state. It does
not index a media library or invent a track title. Metadata is shown to the local
UI and is not automatically written to application logs or settings.

## Recording, video and iPad

Supported iPads are not inherently excluded from HFP. Apple's public profile
list includes HFP 1.8 on iPad Air and later, alongside A2DP and AVRCP.

The mobile app controls its audio session and input route. It must support and
select a Bluetooth HFP input for BlueHead to supply the computer microphone.
Phone calls are one use of that route; supported audio-recording, video-recording
and VoIP apps may also request it. BlueHead cannot force every app to use it.
An app can also play through A2DP while retaining a different microphone input.

Apple's newer high-quality AirPods recording APIs are separate capabilities.
Standard HFP implementation does not make BlueHead an AirPods device. Neither a
Bluetooth 5.3 radio nor generic iPhone/iPad profile support establishes LC3-SWB
support. LC3-SWB requires actual compatible HFP codec negotiation; otherwise the
peer must use its mutually supported fallback. Do not label an Apple model as
LC3-SWB-compatible without a successful observed negotiation and audio test.

## Offline evidence

`tests/test_avrcp.py` exchanges real SDP/L2CAP/AVCTP/AV/C frames through Bumble
virtual controllers. It covers two peers, Unicode metadata continuation,
notifications, volume feedback rules, bounds, timeout/cancellation, collision
recovery, late replies and ACL reconnects.

`tests/test_avrcp_integration.py` exercises the Receiver and Engine together. It
checks Apple-style 44.1 kHz SBC and MPEG-2 AAC-LC capability negotiation, native
decoded PCM, AVDTP suspend/resume with AVRCP still connected, volume/gain
separation, action validation, metadata log exclusion and repeated peer
reconnects. Synthetic source audio and virtual controllers are not substitutes
for Apple firmware, an application's route policy or real RF behavior.

The existing audio regression suite covers native media, voice and Windows audio
lifetime independently. A passing offline suite does not certify end-to-end
latency or Apple interoperability.

## Real-device acceptance record

Record the exact device model, OS version, app and selected codec. Avoid saving
device addresses, pairing keys, track titles or recordings in a public report.

| Scenario | iPhone | iPad |
| --- | --- | --- |
| First pairing, screen-locked reconnect, forgetting/re-pairing | Pending | Pending |
| AAC and SBC playback, track changes, pause/next/previous | Pending | Pending |
| Standard volume synchronization and local 0..200% gain | Pending | Pending |
| Media output switched to the device and back | Pending | Pending |
| Call/VoIP interruption followed by media playback | Pending | Pending |
| Windows playback-device and microphone hot switching | Pending | Pending |
| Supported recording/video app selecting Bluetooth input | Pending | Pending |
| Siri interruption and return to media | Pending | Pending |
| iPhone/iPad plus another device: independent control and media policy | Pending | Pending |
| Video audio synchronization and long playback | Pending | Pending |

Only replace Pending with a result after testing that exact combination. Current
scope is standards-based interoperability, not Apple/MFi certification or a claim
that every accessory-design recommendation has been met. BlueHead does not
borrow Apple's manufacturer identifiers or advertise private accessory features.

## Primary references

- [Apple: Bluetooth profiles that iOS and iPadOS support](https://support.apple.com/en-us/102842)
- [Apple Accessory Design Guidelines](https://developer.apple.com/accessories/Accessory-Design-Guidelines.pdf), 2026-09-21 edition: sections 53, 57.11.5 and 57.12
- [AVAudioSession: allowBluetoothHFP](https://developer.apple.com/documentation/avfaudio/avaudiosession/categoryoptions-swift.struct/allowbluetoothhfp)
- [AVAudioSession: allowBluetoothA2DP](https://developer.apple.com/documentation/avfaudio/avaudiosession/categoryoptions-swift.struct/allowbluetootha2dp)
- [Apple WWDC25: audio recording capabilities](https://developer.apple.com/videos/play/wwdc2025/251/)
- [AVRCP implementation notes](AVRCP.md)

Maintenance regression fixtures and stress harnesses mentioned here are not part of the public build inputs. For the built-in product diagnostics available from a clean checkout, see [DIAGNOSTICS](DIAGNOSTICS.md).

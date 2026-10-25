# HFP voice compatibility

BlueHead acts as the Hands-Free unit (HF). The connected phone or tablet is the
Audio Gateway (AG). This is Bluetooth Classic HFP over RFCOMM and eSCO; LC3-SWB
does not require, enable, or imply LE Audio support.

## Codec selection

The native engine implements these voice formats:

| HFP codec ID | Negotiated format | Encoded frame | Host PCM |
| --- | --- | --- | --- |
| 1 / CVSD | 8 kHz mono | Controller CVSD; HCI carries 8 kHz S16 PCM | 48 kHz mono S16 |
| 2 / mSBC | 16 kHz mono, 7.5 ms | 57 bytes + 2-byte H2 + 1 padding byte | 360 samples per frame |
| 3 / LC3-SWB | 32 kHz mono, 7.5 ms | 58 bytes + 2-byte H2; no padding | 360 samples per frame |

HFP 1.9 is advertised in SDP. Its Wide Band Speech and Super Wide Band Speech
feature bits reflect the advertised codecs. Codec negotiation uses `AT+BAC` and
`+BCS` / `AT+BCS`; the AG chooses the codec. Bluetooth version numbers alone do
not establish whether a particular device, operating system, or app supports
LC3-SWB. A device offering only mSBC or CVSD can continue using that format.

BlueHead prepares a pending codec before replying to `+BCS`, because an AG may
request eSCO before its `OK` has resumed the Python protocol coroutine. A failed
or cancelled transaction clears the pending choice. An incompatible SCO request
is rejected; BlueHead does not silently reinterpret compressed frames as CVSD.

When the controller rejects a codec's synchronous-link parameters, BlueHead can
withdraw that codec for the current peer and service-level connection: LC3-SWB
falls back to an mSBC/CVSD offer, then mSBC to CVSD. Each step sends `AT+BAC`
followed by `AT+BCC`; only a subsequent AG `+BCS` and successful link establish
the new format. Busy resources and normal cancellation do not reduce codec
capabilities. Repeated failures for the same codec cannot cause a retry loop or
change another peer's offer. Reconnection restores the original capabilities.

An AT `ERROR` restores the previous offer, synchronizing it with the AG if BAC
had already succeeded. An AT timeout or failed restoration requires a new
service-level connection; a late `OK` could otherwise be mistaken for a later
command's reply. A delayed failure from an older codec is ignored once a newer
`+BCS` selection is already in progress.

Each peer's AT receiver has a 16 KiB accumulated-input limit, a 4 KiB response
line limit and 128 entries in each response queue. RFCOMM fragmentation and
shared CRLF delimiters are retained. The pinned Bumble parser still parses AT
parameters; malformed input and excessive indicator-range expansion terminate
only that peer's service-level connection. Diagnostics use predefined error
messages rather than raw AT input or caller numbers. The owner closes the DLC;
`stop()` also wakes pending commands and prevents queued commands being sent
after closure. A fresh SLC starts with clean parser and codec-offer state.
Commands also have an absolute deadline and a 128-response cumulative limit,
so a slow stream of partial replies cannot keep a command or its result list
growing indefinitely. An interrupted command closes the SLC before a late
`OK` could acknowledge a different request.

Media playback, voice calls, voice assistants, and recording applications may
request different audio routes. A voice link is accepted independently of call
indicators, so an application requesting microphone input can use the same
duplex path. The application and mobile OS still control whether they expose
that Bluetooth input route.

## Native audio path

`native/voice_engine.c` owns codec state, H2 synchronization, fragmented input,
sequence loss tracking, packet-status handling and the bounded PCM queue.
`bluehead/voice.py` only moves owned byte buffers and serializes lifetime. Every
voice codec supplies mono signed 16-bit PCM at 48 kHz to the native mixer and
accepts the same format from the native microphone/system-audio mix.

Google liblc3 1.1.3 performs LC3-SWB encoding, decoding and packet-loss
concealment. mSBC uses the pinned FFmpeg SBC codec and SpeexDSP conversion.
CVSD conversion uses SpeexDSP, while the Bluetooth controller performs CVSD
coding. mSBC/CVSD concealment uses an isolated SpanDSP PLC library. It has no
audio-device access and adds no network service.

`bluehead_plc.dll` is a replaceable LGPL-2.1 library, dynamically linked by
`bluehead_voice.dll`. Its upstream source, license, exact commit and portable
build adapter are in `vendor/spandsp-plc`. `tools/build_voice.py` reproduces the
DLL without modifying the upstream concealment algorithm. Google liblc3 is
Apache-2.0 and remains outside that LGPL dynamic-library boundary.

Both mSBC and LC3-SWB use transparent eSCO. They have identical 60-byte / 7.5 ms
transport throughput. A controller with a working transparent host eSCO data
path can carry software LC3-SWB without a hardware LC3 codec. Actual support
still depends on controller features, firmware, USB isochronous endpoints,
correct scheduling and the AG's negotiated capabilities.

## Offline verification and acceptance

Run `python tools/build_voice.py` after building the pinned FFmpeg libraries.
Run `python -m pytest tests/test_native_voice.py tests/test_hfp_compat.py` for
actual codec, H2, PLC, virtual RFCOMM and duplex callback coverage. These tests
do not open a physical microphone, speaker or Bluetooth adapter.

`python tools/voice_stress.py --minutes 30 --report work/voice-stress.json` runs
30 simulated minutes per codec as fast as the real native algorithms execute.
The report records complete frame counts, checksums, loss and bounded-queue
statistics. It is an offline timing/robustness check, not a radio-range,
end-to-end latency or perceptual quality measurement.

Physical iPhone/iPad, Android and HarmonyOS acceptance must check the actual
selected codec, bidirectional audio, recording-only routing, media restoration
after voice use and device switching. Successful virtual-controller checks do
not constitute Bluetooth qualification or prove every device supports LC3-SWB.

Protocol references: [HFP 1.9](https://www.bluetooth.org/DocMan/handlers/DownloadDoc.ashx?doc_id=574215),
[HFP test suite](https://files.bluetooth.com/wp-content/uploads/dlm_uploads/2025/02/HFP.TS_.p28.pdf),
[LC3 specification and test software](https://www.bluetooth.com/specifications/specs/low-complexity-communication-codec-1-0/).

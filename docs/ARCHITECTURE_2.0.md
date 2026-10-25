# BlueHead 2.0.0 implementation status

Status: native audio migration and the compatibility features below are implemented in a candidate undergoing final package validation. User hardware acceptance is required before publishing. The installed and published 1.2.4 builds remain the rollback baseline. This document records implementation boundaries, not radio qualification or a claim of Apple certification.

For the current component and recovery diagram see [ARCHITECTURE.md](ARCHITECTURE.md); for actual offline clock evidence see [AUDIO_AUDIT_2.0.md](AUDIO_AUDIT_2.0.md).

## Implemented boundaries

The desktop remains C#/WPF with its compact controls and driver recovery workflow. It targets Windows .NET Framework 4.8 x64, rather than shipping a private .NET 10 runtime. Driver replacement still uses a separate, explicitly elevated helper; normal audio reception runs without admin.

Bumble remains the pinned Bluetooth signalling implementation: discovery, pairing, SDP, AVDTP, AVCTP/AVRCP and RFCOMM/HFP. Python 3.13 owns protocol sessions, USB packet dispatch and control. Encoded packets and state can still cross Python; this is not a complete C Bluetooth stack or a zero-GIL transport.

The native audio modules own media depacketization/decoding, rate conversion, bounded PCM queues, clock correction, gains, mixing, endpoint rendering/capture, voice encoding/decoding, concealment and H2 framing. Rendering stays in the receiver process so process-excluding loopback excludes received audio. Native endpoint callbacks do not allocate, wait on producer locks, call Python or do file/network I/O. Control/decoder workers may allocate during configuration, with bounded packet and PCM backlogs.

Mature components are pinned with license/hash records: FFmpeg for media/mSBC, SpeexDSP for endpoint/codec rate conversion, miniaudio for WASAPI lifecycle, Google liblc3 for LC3-SWB, and separately replaceable SpanDSP PLC. PyAV/NumPy remain development test oracles; av/NumPy/sounddevice/PortAudio are excluded from the production runtime. The package retains required Framework, COM, WinRT, USB, driver-recovery and licensing components.

## Independent voice and capture clocks

Production 48 kHz capture selects the native adaptive uplink. Microphone and computer-audio buses each have their own queue and smooth integral controller, followed by the existing 64-tap/1024-phase native sinc conversion. An adaptation of PipeWire's pinned MIT Bluetooth controller uses a 3-second smoothing period and bounded +/-1000 ppm conversion. It matches samples to the SCO consumer clock while preserving the encoder's fixed output frame size and transport cadence. Muting consumes queued PCM, so unmuting does not replay stale capture. The legacy byte FIFO is an explicit low-rate test interface.

The active HFP downlink also receives this correction; it is no longer excluded from rate matching. The existing media proportional controller and +/-500 ppm limit remain unchanged. These are distinct source clock domains even when every nominal format says 48 kHz.

Voice/capture buffer settings are effective queue targets at render centre. Pre-render observations and startup thresholds add half the current render quantum: 5 ms for a 10 ms playback render, or 3.75 ms for a 7.5 ms SCO read. Actual remaining queue, startup/recovery prefill, discarded frames, starvation and signed correction are reported separately. Packet granularity and device, codec, OS and radio buffering are additional; a buffer setting is not measured end-to-end latency.

Twenty real-native virtual-time clock cases passed: four independent dual-bus 30-minute cases, plus sixteen downlink 10-minute cases over 20/30/40/60 ms buffers, all at +/-100 or +/-500 ppm with +/-3 ms arrival jitter. No drops or underruns occurred in those cases. Additional tone and boundary tests, exact queue ranges, the initial superseded FIFO failure and reproducible evidence are documented in [AUDIO_AUDIT_2.0.md](AUDIO_AUDIT_2.0.md). This does not replace hardware testing or guarantee continuity outside the tested conditions.

## Implemented compatibility paths, hardware acceptance pending

* Standard Apple media uses AAC-LC/LATM and SBC at 44.1/48 kHz, standard HFP microphone routing, interruption/resume handling and accurate SDP/EIR capabilities. No proprietary Apple service or fabricated vendor identity.
* AVRCP provides per-peer playback commands, metadata/event handling, bounded continuation and timeout/cancellation cleanup. Standard 0-127 absolute volume remains independent from BlueHead's 0-200% gain. Unsupported browsing and cover-art features are not advertised.
* HFP 1.9 provides LC3-SWB codec 3, 32 kHz mono, 7.5 ms/58-byte frames plus H2 and transparent eSCO host transport. mSBC/CVSD remain negotiated fallbacks. This is not LE Audio; Bluetooth version alone proves neither codec nor controller support.

## Preserved invariants and release gate

Keep duplex voice, media after hangup, input/output hot switch, default-device following, per-device/master gains and mutes, process-excluding computer-audio mixing, multipoint, automatic single-device handoff and recoverable driver replacement. Delayed opens and disconnected peers must not write into a new session. Device failures report a recoverable state instead of corrupting buffers. Native and desktop lifetime/failure checks exercise these boundaries; hardware behavior remains in the candidate acceptance checklist.

The candidate includes source, build instructions, release notes, license and dependency material. Before finalizing a package, record the actual build, offline/regression/UI outcomes, package inventory and remaining hardware checks in its `VALIDATION.md`, following [BUILDING.md](BUILDING.md) and [TESTING.md](TESTING.md). The final frozen engine and desktop must be checked, not only source modules. Publish to GitHub only after user acceptance; exclude private pairing, logs, local driver backups, toolchains and build directories from the public source archive.

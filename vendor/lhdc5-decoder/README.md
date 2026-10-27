# LHDC V5 decoder source

Pinned decoder math by William Ho (WillyBilly06), declared Apache-2.0 by the
[upstream README](https://github.com/WillyBilly06/LHDC-V5-Decoder/tree/cf857ce1a1429965e161e54adbc3f293fd9ce273).
Only required decoder sources are included. Original Git blobs are unchanged;
`origin.json` records every hash. BlueHead builds its verified native adapter from
these sources in a temporary directory. ESP-IDF, sample projects, test encoders
and proprietary binaries are not included.

The adapter must run in one isolated codec process per peer. Upstream contains
module-wide synthesis and transform state and is not a multi-instance library.
The production adapter is in `native/codecs/lhdc5_{stream,media}.c`, built by
`tools/build_lhdc5.py`. Build-time changes verify the original blobs first, then
fix undefined shifts, reject exhausted entropy data and failed second channels,
and publish normalized float PCM with the reference synthesis window. Supported
negotiation is 44.1/48/96/192 kHz, 16/24-bit stereo. Optional lossless, AR/JAS and
metadata extensions are not advertised. Signal references, malformed streams,
RTP fragmentation and process isolation are covered by `tests/test_lhdc5_*`;
see `docs/LE_AUDIO.md` for validation boundaries. No proprietary encoder or SDK is
required at runtime; Bluetooth/codec product certification is not asserted.

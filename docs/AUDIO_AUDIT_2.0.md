# BlueHead 2.0 native audio audit

**Current status: the clock-mismatch and capture-lifecycle findings below are fixed in 2.0.0 and pass the stated offline regressions.** Review and measurements: 2026-10-26. The user has also confirmed basic audio and AVRCP operation on revision `bf80c7a`; the same release's `VALIDATION.md` records the exact verification scope.

The tests exercise the compiled native mixer, media decoder, microphone/system uplink and capture worker in virtual time. They do not open physical audio or Bluetooth devices. Passing these tests does not establish radio interoperability or end-to-end latency. See [TESTING.md](TESTING.md) for acceptance scope and [DIAGNOSTICS.md](DIAGNOSTICS.md) for diagnostic operating modes.

## Implemented clock matching

The production 48 kHz uplink uses `CallRouter.configure(48000, adaptive=True)`. The microphone and computer-audio buses have independent bounded native queues, smoothed observations and integral rate controllers. Each uses the existing 64-tap, 1024-phase native sinc interpolator before gain, mixing, limiting and mono S16 output. No Python numerical processing or new DSP allocation occurs in the render operation. Legacy byte-FIFO mode remains for explicit low-rate test oracles; it is not the production 48 kHz capture path.

The active HFP downlink now uses the same class of clock matching rather than being excluded from correction. The voice/capture controller has a 3-second smoothing period and a bounded +/-1000 ppm correction. The previous media controller retains its proportional factor and +/-500 ppm limit. This audit tests +/-100 and +/-500 ppm voice/capture mismatch; the configured bound is not a claim that every clock, interruption or radio loss can be corrected.

The controller adapts the MIT-licensed PipeWire Bluetooth [rate controller at the pinned revision](https://github.com/PipeWire/pipewire/blob/73974598fafeea9491f0f36a31bdaef2352dbd4f/spa/plugins/bluez5/rate-control.h). The original header, license, revision and SHA256 hashes are retained in `vendor/pipewire-rate-control/`. Smoothing avoids following every packet burst; the integral term removes the permanent queue displacement of a pure proportional controller. It changes the conversion ratio smoothly, not the H2 frame cadence. Encoded-frame remainders remain owned by the voice transport.

### Buffer target and reported latency

Voice/capture `buffer_ms` is the effective queue target at the **centre of a render quantum**. The pre-render threshold and controller target add half that quantum. For a 10 ms downlink render, a 20 ms setting therefore needs 25 ms before rendering; a 30 ms uplink target with 7.5 ms SCO reads needs 33.75 ms. Packet granularity and the initial absence of input may add further startup waiting. This preserves a safety margin without silently changing a 20 ms setting to a 60 ms target.

`queued_frames` is the actual remaining queue, normally observed after a read; it need not equal the centre target. Startup/recovery waiting is counted separately as `prefill_frames`. Actual starvation increments `underruns`, and overflow/discard increments `dropped_frames`; consuming 359 or 361 input samples for 360 output samples is normal clock correction, not a discarded audio block. The uplink snapshot identifies this convention as `buffer_reference=render_center` and reports each bus's queue, drops, underruns, prefill and signed `drift_ppm`.

These targets exclude endpoint periods, Windows conversion, codec buffering, USB/radio transport and peer buffering. Neither the configured target nor a native processing benchmark is an end-to-end Bluetooth latency measurement.

## Verified clock results

`tests/test_uplink_clock.py` contains **20 long clock cases**, using real native PCM processing, independent clocks and deterministic +/-3 ms arrival jitter:

| Path | Cases and simulated duration | Observed result |
| --- | --- | --- |
| Two independent capture buses -> SCO | 4 cases, microphone +/-100 or +/-500 ppm and computer audio at the opposite mismatch; 30 minutes each; 10 ms capture / 7.5 ms read | Both buses: 0 dropped frames, 0 underruns; startup prefill 1800 output frames each |
| HFP downlink -> playback | 16 cases: +/-100 and +/-500 ppm at each 20/30/40/60 ms target; 10 minutes each; 7.5 ms input / 10 ms render | All cases: 0 dropped frames, 0 underruns; final correction error <=9 ppm |

After the first simulated minute, sampled uplink queues ranged from 877 to 1644 frames across both buses and all four cases. Final correction error was at most 12 ppm. The downlink sampled post-render queue ranges were:

| Effective target | Remaining queue range across four clock cases |
| --- | ---: |
| 20 ms | 397-1044 frames |
| 30 ms | 877-1524 frames |
| 40 ms | 1357-2004 frames |
| 60 ms | 2317-2964 frames |

Queue extrema are sampled diagnostics, not a proof of an exact hard bound. Drop and underrun counters cover every native render in these tests. The smallest 20 ms / -500 ppm / +/-3 ms jitter case also passes from startup, rather than discarding initial failures from its counters.

Four additional clock boundary cases in `tests/test_native_boundaries.py` pass: three single-bus 30-minute cases at -500/0/+500 ppm, and one 120-second case with 10 ms capture, 7.5 ms reads and +/-2.5 ms deterministic jitter. They use native drop/underrun counters, rather than inferring a loss from SRC queue-size changes.

Four tone cases cover +/-500 ppm at 1 kHz and 20 kHz. The reference is generated at the **actual** source rate, `48000 * (1 + ppm / 1e6)`. Each runs for 180 simulated seconds and checks the final 30 seconds: FFT peak error <1 Hz, RMS error <2.5%, and no underrun. A nominal-48-kHz reference would incorrectly turn the intentionally different source clock into a tone-frequency error.

An additional local 300-second steady-state profile, sampled after 60 seconds, observed -522..-484 ppm (mean -500.92) for a -500 ppm source and 484..530 ppm (mean 501.55) for +500 ppm; both had zero drops/underruns. This supplementary profile is not a separately distributed test tool. The required long-run and tone gates above are all in the tracked tests.

## Earlier finding: byte FIFO drift (fixed)

The initial, superseded `audio_uplink.c` path capped its byte FIFO at 90 ms and trimmed to 60 ms. It read exactly the requested byte count and filled shortages with zeroes, without matching independent capture and SCO clocks. The following table records that **initial failure**, not the current adaptive implementation:

| Capture clock difference | Bulk trims | Largest trim | Zero-filled samples | Peak FIFO |
| --- | ---: | ---: | ---: | ---: |
| -500 ppm | 0 | 0 | 42,480 / 885 ms | 27.5 ms |
| 0 ppm | 0 | 0 | 0 | 30 ms |
| +500 ppm | 26 | 1,560 / 32.5 ms | 0 | 90 ms |

These were 30-minute native FIFO runs with 480-sample capture blocks, 360-sample reads and an initial 30 ms reserve. Positive drift caused the first trim around 125 seconds, then about every 65 seconds. More buffer would only delay that pattern. The initial mixer also excluded the active voice stream from correction. Enabling its old proportional controller alone would leave a 25 ms equilibrium queue displacement at -500 ppm, consuming most of a small voice buffer's margin. Independent integral clock matching and the centred render observation address both findings; the current tests now pass.

## Capture lifecycle and other verified boundaries

The initial `CaptureStream.start()` retained an unstarted `Thread` when `Thread.start()` failed. Later cleanup tried to join it before releasing the native endpoint. The corrected failure path clears that worker reference; its injected-failure regression confirms removal from the endpoint owner.

The complete `tests/test_native_boundaries.py` run passed 12 cases. Besides the four clock cases above, it verifies:

- Concurrent mixer publication, rendering, clear, remove and slot reuse remain finite and bounded, with renderer-owned sample history.
- Holding a producer critical section does not block rendering of published PCM; the check runs in an isolated Win64 subprocess.
- Bounded malformed RTP, extension/padding/fragment lengths and aptX input do not overrun native output buffers; canaries remain unchanged.
- A failed capture consumer marks the stream unhealthy; stopping and replacing its drain worker does not require an endpoint-list change.

The deterministic malformed-input sample is not exhaustive fuzzing or sanitizer proof. These regressions do not cover long hardware calls, radio scheduling, driver changes or Apple interoperability. Actual device and application checks are recorded separately in [TESTING.md](TESTING.md) and the release's `VALIDATION.md`.

## Reproduction and evidence

Run from the source root with development dependencies and matching native libraries built as described in [BUILDING.md](BUILDING.md). Stop all processes which load a source DLL before rebuilding it. No hardware opening is required for these commands:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_uplink_clock.py tests/test_native_boundaries.py -q --tb=short --basetemp work/clock-audit-reproduction -p no:cacheprovider
.venv\Scripts\python.exe -m tests.test_native_boundaries | Set-Content work/uplink-clock-boundaries.json -Encoding UTF8
```

The first command runs 26 clock/tone/uplink cases plus 12 boundary cases. Each of the 20 long clock cases writes its own JSON below the specified pytest temporary directory, including duration, mismatch, jitter, queue range and native counters. The second command prints the three current adaptive single-bus 30-minute diagnostics. It does not rerun the superseded FIFO path.

Collect the 20 long-case records without any private local script:

```powershell
$clockReports = Get-ChildItem work/clock-audit-reproduction -Recurse -Filter *.json |
    Where-Object { $_.Name -match '^(uplink|downlink)-.*\.json$' } |
    ForEach-Object { Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json }
$clockReports | ConvertTo-Json -Depth 8 | Set-Content work/native-clock-evidence.json -Encoding UTF8
```

The development run also aggregated the 20 long-case records and supplementary profile into `work/native-clock-audit.json`. That ignored local evidence file and the ad hoc profile script are not source or runtime dependencies; the tracked tests and commands above reproduce the required gates. Release test commands, outcomes, package hashes and unperformed hardware checks belong in the same release's `VALIDATION.md`.

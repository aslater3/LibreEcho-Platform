# Music feature producer (Platform #44/#45)

`music_features.h` turns the twelve normalized band levels already produced by
`audio_visualizer.c` into the frozen **version-2** visualizer feature frame that
LibreEcho-UI replays for the scene director (UI #64) and the event-driven
transition effects (UI #65).  `audio_engine.c` owns the transport: it packages
one frame from the final post-limiter mono programme every second PCM period and
sends it to the LED daemon on the existing owner-scoped socket.

This unit **publishes bounded perceptual evidence, not exact instrument
recognition or genre detection**.  The values are fixed-point strengths, not
calibrated acoustic units.

## Wire contract (frozen)

Envelope and command name are unchanged from version 1:

```json
{"v":1,"id":2,"cmd":"visualizer","args":{ ... }}
```

A version-2 frame carries:

| Field | Range | Meaning |
|---|---|---|
| `action` | `"frame"` | unchanged |
| `owner` | `"music"` | unchanged owner scope |
| `feature_version` | `2` | selects the perceptual contract |
| `session` | nonzero `uint32` | stable per producer lifetime, see reset below |
| `seq` | `uint32` | monotonic within a session, starts at 0 |
| `timestamp_ms` | `uint32` | monotonic milliseconds, see clock below |
| `levels` | 24 lowercase hex digits | the existing twelve LED levels, unchanged |
| `brightness` | 0..100 | the existing **LED master** brightness, unchanged |
| `energy` | 0..255 | short-term loudness |
| `warmth` | 0..255 | low-band weight |
| `brightness_axis` | 0..255 | spectral centroid; **distinct** from LED master brightness |
| `density` | 0..255 | occupied band fraction |
| `transientness` | 0..255 | fast spectral flux |
| `groove` | 0..255 | rhythmic regularity confidence |
| `build` | 0..255 | sustained multi-second rising trend |
| `spaciousness` | 0..255 | empty-spectrum fraction |
| `loudness_fast` | 0..255 | ~0.2 s loudness envelope |
| `loudness_slow` | 0..255 | multi-second loudness envelope |
| `onset_low` | 0..255 | positive flux in bands 63–250 Hz |
| `onset_mid` | 0..255 | positive flux in bands 400–1600 Hz |
| `onset_high` | 0..255 | positive flux in bands 2500–11000 Hz |
| `beat_strength` | 0..255 | latest beat impulse |
| `beat_confidence` | 0..255 | tempo confidence, decays when beatless |
| `beat_phase` | 0..65535 | phase within the current beat |
| `bpm_x100` | 0..30000 | tempo ×100, `0` when unconfident |
| `novelty` | 0..255 | slow-spectrum movement rate |
| `event_strength` | 0..255 | strength of the most recent structural event |
| `events` | bitmask | see below |

Event bits (`music_features.h`):

| Bit | Value | Name |
|---|---|---|
| 0 | 0x0001 | `kick` |
| 1 | 0x0002 | `snare` |
| 2 | 0x0004 | `high` |
| 3 | 0x0008 | `fill` |
| 4 | 0x0010 | `build` |
| 5 | 0x0020 | `reentry` |
| 6 | 0x0040 | `breakdown` |
| 7 | 0x0080 | `section` |
| 8 | 0x0100 | `drop` |

Every field is always emitted explicitly; a consumer never relies on a
provider default.

## Reset, clock and staleness

- **Session** is a nonzero `uint32`, derived from the producer start.  It folds
  the full monotonic clock in nanoseconds, the process id and 32 bits of kernel
  entropy (`music_session_id.h`), so two rapid producer restarts -- even inside
  one millisecond -- get different ids.  It changes only on producer start or
  reset (an engine restart), so a consumer can reject frames from a previous
  producer run.
- **seq** is monotonic within a session and restarts only with a new session.
- **timestamp_ms** is monotonic milliseconds from the analysed-period clock
  (`update_count * 2048 * 1000 / 48000`), not the wall clock: it is deterministic
  and monotonic, and it never moves backwards.  The clock is tracked in 64 bits,
  but the wire field is a frozen `uint32`, so the producer rotates the session
  before the value would cross the 32-bit boundary (about 49.7 days of
  continuous playback): `music_feature_transport_begin_tick()` starts a fresh
  session (new nonzero id, `seq` 0, clock 0) on the wrapping tick instead of
  publishing a wrapped `timestamp_ms` in the live session.
- A consumer rejects a stale or reordered frame when `seq` does not advance or
  `timestamp_ms` moves backwards within the current session.

## Packet size

`MUSIC_FEATURES_FRAME_MAX` is 768 bytes.  The engine writes into a
`MUSIC_FEATURES_FRAME_MAX` stack buffer and sends exactly one best-effort
zero-wait datagram-style write; a measured version-2 frame is ~527 bytes, well
under the AF_UNIX stream buffer the LED daemon reads, so framing is never split
by a short buffer.  LED socket work stays nonblocking; a missing or busy LED
daemon drops a frame and never delays PCM.

## Compatibility switch

`LIBREECHO_AUDIO_VISUALIZER_VERSION=1` in the engine environment keeps the
legacy version-1 frame (`levels` + `brightness` only, no feature fields) for a
consumer that has not migrated.  Version 2 is the default.  Version 1 remains
emitted correctly by `music_features_format_frame(..., feature_version=1, ...)`.

## Analysis model

- Reuses the existing 12-band, Q28, fixed-point filter bank.  Features are
  derived from the **unshaped** normalized band levels, so they do not inherit
  the LED display AGC applied by `shape_display_levels()`.
- Loudness uses a fast (~0.2 s) and a slow (multi-second) envelope.
- `build` is the multi-second rising trend of the slow loudness axis
  (a delayed-slow comparison), so a steady loud programme has no build and a
  single transient never appears in the slow axis.
- `novelty` is the movement rate of the slow spectrum, primed on the first
  period so a steady programme settles to near zero.
- Tempo is estimated by autocorrelation of the onset envelope over lags
  spanning ~44–234 BPM; `bpm_x100` is published only while confidence is
  sufficient, and confidence decays when the signal is beatless.
- `kick`/`snare`/`high` come from per-group adaptive onset thresholds with a
  short refractory; these are ordinary beat/transient evidence, not structural
  events.
- Structural gates (`build`, `breakdown`, `reentry`, `section`, `drop`, `fill`)
  are multi-frame and cooldown-guarded.  A `drop` requires an armed build plus a
  dip-and-recovery or a high-impact downbeat, so an isolated loud hit never
  opens it.  Event flags are latched for a few updates so an event that lands
  between two emitted frames is still delivered.

## Bounded resources and determinism

- No allocation, no floating point in the analysis, no wall clock, no
  randomness.  `struct music_features_state` is 312 bytes and
  `struct audio_visualizer` is 512 bytes on the host (`x86-64`); fixed-size
  rings only.
- The same synthetic PCM stream yields a bit-identical feature sequence on every
  run and host.  Host analysis cost is ~100 µs per 2048-frame period.

## Tests

```sh
bash tools/mt8163-arm32/airplay/test_music_features.sh
bash tools/mt8163-arm32/airplay/test_music_feature_trace.sh
bash tools/mt8163-arm32/airplay/test_music_transport_wrap.sh
python3 tools/mt8163-arm32/airplay/test_music_feature_packet.py
```

- `test_music_features.sh` drives the real analysis path with synthesized PCM:
  stable tempo clicks, differentiated low/mid/high bursts, warmth/brightness
  axes, build/drop, quiet gap and re-entry, a section change, a fill accent, a
  steady dense false-positive, beatless confidence decay, bit-identical replay,
  the v1/v2 packet contract, bounded memory and host runtime.
- `test_music_feature_trace.sh` emits a deterministic JSONL trace of real
  version-2 packets for the cross-repository roundtrip.
- `test_music_transport_wrap.sh` drives the real transport at the production
  period/rate one period below the 32-bit clock boundary and proves the session
  rotates there (fresh id, `seq` 0, clock restarting), no timestamp moves
  backwards inside a session, and the emitted frame keeps the frozen field set.
- `test_music_feature_packet.py` compiles and runs the emitter twice, requires
  identical output, and validates every packet against this contract.

`MF_DEBUG=1` on `test-music-features` dumps a per-period CSV trace for
calibration.

## Offline trace and regression corpus (#46)

`music_trace.c` runs the production producer path (the same
`audio_visualizer_process_features()`, transport and frame formatter the audio
engine uses) over a recording and writes the version-2 frames as JSONL. The
same input always gives a byte-identical trace.

```sh
cc -O2 -std=c99 audio_visualizer.c music_trace.c -lm -o music-trace
ffmpeg -i song.flac -ar 48000 -ac 2 -c:a pcm_s16le song.wav   # 48 kHz 16-bit PCM WAV
./music-trace song.wav song.jsonl
./music-trace --gain-db -20 song.wav song-quiet.jsonl          # same programme, quieter
```

`corpus/gen_corpus.py DIR` writes a synthetic corpus (click tracks at 90, 120
and 150 BPM, a beatless pad, white noise, a sweep, silence and a -20 dB copy)
with integer synthesis, so every host produces identical files and no audio is
committed. `test_music_trace.py` checks it in CI: silence is inert, beatless
material reports no tempo, click tracks get a confident tempo, and level does
not move tempo or confidence, and 90, 120 and 150 BPM are reported at the
right metrical level. One known gap is an expected failure until fixed: energy
and density follow the playback level because the engine analyses the
post-volume mix (#44).

To evaluate on real music, keep the recordings and a list of them outside the
repository and run `music-trace` over them; never commit copyrighted audio.

The tempo search correlates a detrended onset envelope (each value minus its
trailing 8-frame mean), so a smooth or slowly swelling envelope no longer
produces a confident tempo on beatless material. A periodic onset train also
correlates at two and three periods, so when the lag a half or a third as long
scores at least 80% (`MF_METRICAL_RATIO_Q8`) of the best lag, the faster level
is reported.

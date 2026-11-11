# AirPlay 2 image inputs

The image packages AirPlay 2 support by default, but the runtime controller
leaves both processes stopped until the UI integration toggle is enabled.

Pinned upstream source inputs for the ARMHF build are:

- Shairport Sync 5.6.2, commit `0a34fbda83724cc06266aadcf7c88acc8fef4e7d`
- NQPTP 1.2.9, commit `ab8fb67483411640fd7f1e193889482f2421c37c`

Shairport Sync must be configured with `--with-airplay-2`, the raw pipe and
metadata-pipe backends, OpenSSL, FFmpeg, libplist, libsodium, libgcrypt, UUID
and Avahi. Track metadata is streamed through a bounded runtime FIFO; cover
art is disabled and no media metadata is written to persistent storage. The Avahi runtime
closure also includes D-Bus and its glibc/systemd support libraries. NQPTP
must run before Shairport Sync when the integration is enabled. The pipeline
keeps the ARMHF dependency sysroot pinned and separate from the target's small
musl userspace: NQPTP is static ARM32, while Shairport Sync is ARM32
glibc-linked and ships with its audited loader/library closure. FFmpeg is built
as a small static audio-only subset so the image does not inherit the host's
full codec dependency tree.

The device's 3.18 ASoC driver is usable through TinyALSA but returns
`ENOTTY` for the libasound probing ioctls used by Shairport's ALSA backend.
The payload therefore uses Shairport's raw named-pipe backend. The
`libreecho-airplay-audio` process is now only a producer: it forwards decoded
S16_LE/48 kHz/stereo PCM to the dedicated `airplay-media.pcm` bus and never
opens ALSA. The bridge's FIFO lifetime is not a playback session: Shairport's
`--start <token>` hook atomically creates a fresh regular `airplay.active`
marker containing that play's 32-digit lowercase hex token. Its first 16
hex digits are a strictly increasing monotonic start tick; the remaining 16
are random. A bounded runtime high-water mark rejects delayed or repeated
start hooks even after a newer stop. `--stop <token>`
removes it and the callback/ack files only when the token matches. The pinned
Shairport 5.6.2 patch creates a fresh token for every play and passes it to
start, stop and `--set-volume <token> <dB>`; pre-start volume is retained for
the first playback callback. Untagged, malformed and stale start/stop/volume
hooks fail closed. The bridge discards input while the marker is absent; bytes on
the dedicated FIFO are never reclassified as generic media. Start and matching
stop hold a bounded shared lock against the bridge's nonblocking read/write:
they clear the marker and sender state, drain predecessor bytes still in the
Shairport input FIFO, then publish a separate fresh 128-bit *engine reset*
token. The engine checks reset requests at most 20 ms apart even when idle,
discards its buffered AirPlay periods and persistent dedicated FIFO, and
atomically acknowledges the exact reset token before the hook publishes a new
marker or returns from stop. A missing acknowledgement fails closed after
about one second; priority buses remain independent. The bridge never holds
the lock over an input wait or an unbounded output write. Only a marker
following the completed reset admits its first PCM; a marker replacement
without a reset is refused. Engine restart begins with AirPlay unarmed rather
than trusting a surviving marker. Host fixtures cover late A callbacks after
B starts; real-device callback ordering and audio remain a release gate.
Pre-reset PCM already handed to ALSA cannot be retroactively unplayed.

A full `airplay-media.pcm` never costs audio (issue #356). The bridge keeps the
unsent remainder (whole 4-byte frames; a trailing partial frame is carried to
the next read), waits for FIFO space outside the shared lock, re-checks the
session marker under the lock before each write, and never closes
`airplay.pcm` or the media FIFO for a transient stall. Held audio is discarded
only when the marker is gone or replaced; an engine that vanishes is waited
out and the media FIFO reopened every 100 ms. Shairport's pipe backend gets a
real `delay()` (`shairport-5.6.2-pipe-delay.patch`), so it does its own drift
correction against the sender timeline and keeps AirPlay group sync. The
number it returns is frames in `airplay.pcm` (read with `FIONREAD`) plus
`downstream_frames` published by the bridge: bridge remainder, media FIFO
(bridge bytes written minus the engine's cumulative bytes read), the engine's
AirPlay queue and the ALSA ring, each fixed-size seqlock page being defined in
`airplay_delay.h` (`<bus root>/airplay.engine-delay` from the engine,
`<airplay.pcm>.delay` from the bridge, `<airplay.pcm>.delay-read` from
Shairport). A snapshot older than 250 ms, a missing or torn page or an
unknown ALSA level makes `delay()` return `-ENODEV`, which is Shairport's
previous no-`delay()` behaviour. The patch also lowers the pipe backend's
default desired buffer to 0.35 s so the queue stays inside the FIFOs.

Diagnostics are RAM only (nothing is written under `/data`). The bridge
logs to stderr, which lands in the RAM-resident `libreecho-airplayd.log`: one
line at most per 10 s for each of `discard`, `reopen`, `session-start`,
`session-end`, `delay-fallback`, `resync` and `flush`, with coalesced counts.
While a session is active `/run/libreecho-audio/airplay.status` (a fixed 1024
byte text file, overwritten in place every 10 s) shows the FIFO fills,
remainder, engine queue, ALSA delay, the `delay()` value Shairport last read,
the running discard, reopen and fallback counts, the flush counts and
Shairport's own resync / late-packet / sync-correction counts.
`flush_engine_unreset` counts flushes in a live session whose engine queue could
not be reset (engine absent, snapshot stale, or the reset not acknowledged). A
page that is only caught mid-update is reported as `engine-busy` (the page
reader backs off for about 1.3 ms in total before giving up) and never skips the
reset.

Session end and replacement (Stop, Stop to a new song, Pause then resume) drop
everything downstream, like a flush: the bridge and the engine drain the held
bytes, the FIFO and the engine's AirPlay queue, and the engine also stops and
re-prepares the ALSA PCM (`pcm_stop` + `pcm_prepare`), so the ring's last ~60 ms
of the ended session never plays after it. The engine notes a boundary when the
marker it last played is gone or replaced; a flush with the marker untouched
(skip, seek) is not a boundary. The drop runs between reading the FIFO and the
next period write, so a new session's audio is never cut, and it is skipped
while another bus or a Sendspin generation shares the ring. While the output
starts (arm, open, prepare, verify, power, first write) the engine publishes a
"starting" snapshot between the steps (not draining, the first period queued,
nothing in ALSA), so `delay()` stays answerable instead of going stale.

Skips, seeks and AirPlay 2 pauses take effect at once. The pipe backend gets a
`flush()` in the same patch: Shairport's player thread calls it (with
`flush_mutex` held) before the next frame after a skip or seek, and AirPlay 2's
buffered-audio thread calls it on pause. One mutex covers `flush()` and each
`play()` chunk (a `play()` that finds the pipe full waits in `poll()` without
it, and drops the remainder of its buffer if a flush happened meanwhile), so
they never race and neither waits unboundedly under the lock. `flush()` bumps
a request counter in the Shairport-written `<airplay.pcm>.counters` page and
waits at most 300 ms for the bridge to answer in `flush_done` of its
`<airplay.pcm>.delay` page. The bridge, under the hook lock, drains
`airplay.pcm`, drops its unsent remainder and has the engine empty its AirPlay
queue and the media FIFO (`reset_engine()`, the discard half of the session
fence), waits up to 150 ms for an engine snapshot that postdates the reset
(different epoch/seq) and republishes the delay before answering (if none
arrives it still answers, counts `flush_engine_late` and publishes the delay as
unavailable, so `delay()` fails with `-ENODEV`, until the engine's next
snapshot), so `delay()` then reports the new, near-empty queue and only the
ALSA ring (about 64 ms) plays out. It does not call `clear_session_state()` and
does not replace the session marker: the volume file stays, and the engine's
master acknowledgement is tied to the marker's identity, so a fresh marker
would leave AirPlay muted until the sender sent another volume. A request older
than the 300 ms wait is answered without discarding (Shairport has moved on
and may already be writing the new track). The same counters page carries
Shairport's resync requests, late-packet drops and frames added or removed by
sync correction, which Shairport itself only logs at debug level 2.
`libreecho-audio-engine` is the sole playback PCM and amplifier owner;
`audiod` alone writes the PCM Playback Volume control. The engine reads back
both codec indices after prepare and again before unmute, refusing to play if
either is not raw 127. It mixes
generic media, AirPlay media, system, announcement, and alarm buses; ducks
both media buses by 12 dB under
higher-priority audio; and renders one mono programme sample with clipping-safe
32-bit arithmetic. It then duplicates that sample into both channels of PCM
`0,23` (`S16_LE`, 48 kHz, 2 channels), selects `Board Channel Config=Stereo`,
and uses normal codec `DACSETUP=0x14` routing. The stock Puffin profile sends
the left/HPL high-pass band to the tweeter and the right/HPR low-pass band to
the woofer. The shared mono bus then uses a re-derived loudness/parametric EQ
followed by an original four-band compressor/limiter approximation at the
Radar stock split frequencies (70/200/3250 Hz). A -3 dB full-band limit
precedes +3 dB OutputTrim; the linked PCM limiter is only a final safety gate.
The vendor's filter-bank slope, detector and compressor timing are not specified
by the stored configuration, so these are independently designed substitutes,
**not a claim of stock acoustic parity**. Human listening and level-matched
spectral/dynamic tests remain required before a release.

This two-channel container is mandatory even though programme semantics remain
mono. The superseded one-channel `MonoRight` / `DACSETUP=0x24` transport made
the woofer play while the tweeter carried noise or silence. LO/LOL/LOR are a
separate line-out branch and must remain Off; enabling them increased tweeter
noise rather than restoring music. `Audio_DacMux_Setting` remains Off.

Active announcement audio also requests a slow green pulse from
the LED daemon. The request is best-effort and owner-scoped, so audio continues
if LED control is unavailable and the previous LED pattern is restored when
the announcement bus becomes idle.

For media-only playback, the engine analyzes the final post-limiter mono
programme actually sent to the Puffin speaker profile. A fixed-point,
12-band filter bank covers 63 Hz through 11 kHz and publishes owner-scoped
visualizer frames to the LED daemon at about 11.7 frames/second. System,
announcement, or alarm activity immediately releases the `music` LED owner so
the higher-priority indication wins; media visualization resumes only after
those buses become idle. LED socket work is zero-wait and best-effort, so a
missing or busy LED daemon cannot delay PCM. Each frame carries the version-2
perceptual feature contract documented in `music-features.md` (loudness and
balance axes, differentiated low/mid/high onsets, a tempo hypothesis with
decaying confidence, and cooldown-gated build/drop/re-entry/section events);
`LIBREECHO_AUDIO_VISUALIZER_VERSION=1` selects the legacy twelve-level frame.

The engine also atomically publishes `/run/libreecho-audio/status.json` with
mode `0644`. It records only playback state (`idle`, `playing`, `system`,
`announcing`, or `alarm`), the highest-priority active bus, and booleans for
all four buses. The file is replaced only when that state changes and carries
no track metadata.

Shairport's pipe uses `ignore_volume_control = "yes"`: UI `airplayd` consumes
the callback outside the chroot and maps sender volume to audiod's logical
`0..100` master. Button/API changes use that same logical master. Audiod must
atomically publish a validated decimal percentage in
`/run/libreecho-audio/master.volume`; the engine reads it every period,
maps `1..100` to codec-equivalent indices `7..103` (`0` is mute), and
smooths Q15 gain before the shared EQ/MBCL. The logical master uses
`-12 - 48 * (1 - x)^1.3` dB, where `x = (percent - 1) / 99`, rounded to
half-decibel codec-equivalent steps. This preserves a quiet -60 dB floor,
tapers the upper slider travel, and caps pre-DSP gain at -12 dB. These are
gain values, not acoustic sound-pressure limits; high-level speaker distortion
has not been ruled out. The physical PCM codec control
remains fixed at index `127` (0 dB) after prepare. Non-muted AirPlay is unity
source gain, sender mute is zero, and generic media alone uses `media.volume`.
An optional `/run/libreecho-audio/reply.volume`, in the same validated
`0..100` form, gives assistant replies their own level: while it exists the
system and announcement buses take the logical gain of that level instead of
the master (smoothed the same way), media, AirPlay, Sendspin and the alarm
bus keep the master, a master of `0` still mutes everything, and the loudness
preset follows the reply level while a reply plays. Absent or malformed means
replies follow the master.
The first AirPlay period waits for matching marker/callback/ack identity;
replacement callbacks retain the last valid gain temporarily without
re-holding audio, then fail closed on prolonged ack loss. Generic media and
priority buses remain independently live. All five buses meet at the same
EQ/MBCL before the PCM write. Source-priority ducking and volume transitions
still require real-device listening and waveform testing; the vendor
compressor is an approximation, not stock-exact.

The Avahi/D-Bus payload remains inside the fixed 16 MiB boot envelope by using
the free range below the DT-reserved RAM console at `0x44400000`.

The normal build prefers `/usr/bin/arm-linux-gnueabihf-g++` and falls back to
the ARMHF C driver when the host has no separate C++ driver; the pinned
AirPlay sources are C. CI or a release builder can override this explicitly
with `LIBREECHO_AIRPLAY_CXX`.

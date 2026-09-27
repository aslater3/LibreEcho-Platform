# AirPlay 2 image inputs

The image packages AirPlay 2 support by default, but the runtime controller
leaves both processes stopped until the UI integration toggle is enabled.

Pinned upstream source inputs for the ARMHF build are:

- Shairport Sync 5.1, commit `d6ac53bf4c6a1ebc55a03177537765ff42dec919`
- NQPTP 1.2.8, commit `c925f27c1fd12e4033ac477e5a405969b0b0260b`

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
Shairport 5.1 patch creates a fresh token for every play and passes it to
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
missing or busy LED daemon cannot delay PCM.

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
maps `1..100` to codec-equivalent indices `67..127` (`0` is mute), and
smooths Q15 gain before the shared EQ/MBCL. The physical PCM codec control
remains fixed at index `127` (0 dB) after prepare. Non-muted AirPlay is unity
source gain, sender mute is zero, and generic media alone uses `media.volume`.
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

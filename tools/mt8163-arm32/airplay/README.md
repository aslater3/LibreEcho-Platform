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
S16_LE/48 kHz/stereo PCM to the shared media bus and never opens ALSA.
`libreecho-audio-engine` is the sole TinyALSA/codec/amplifier owner. It mixes
media, system, announcement, and alarm buses; ducks media by 12 dB under
higher-priority audio; and renders one mono programme sample with clipping-safe
32-bit arithmetic. It then duplicates that sample into both channels of PCM
`0,23` (`S16_LE`, 48 kHz, 2 channels), selects `Board Channel Config=Stereo`,
and uses normal codec `DACSETUP=0x14` routing. The stock Puffin profile sends
the left/HPL high-pass band to the tweeter and the right/HPR low-pass band to
the woofer. A linked peak limiter restores the stock pipeline's +3 dB output
trim without allowing PCM clipping or positive codec gain.

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

Shairport's pipe must use `ignore_volume_control = "yes"` because the external
volume hook owns codec attenuation. Otherwise Shairport attenuates the PCM in
software and the hook applies the same AirPlay attenuation again.
The bridge clears the previous session's AirPlay volume before publishing a new
active marker. The shared engine waits for the new session's first valid volume
callback before arming the PCM; a missing callback therefore cannot fall back
to the device's current volume.
The engine reapplies the physical amplifier controls after the codec starts
DMA. AirPlay dB callbacks update only the media-bus software gain, so an
announcement can remain audible above quiet media without changing the
device-wide codec volume.

The Avahi/D-Bus payload remains inside the fixed 16 MiB boot envelope by using
the free range below the DT-reserved RAM console at `0x44400000`.

The normal build prefers `/usr/bin/arm-linux-gnueabihf-g++` and falls back to
the ARMHF C driver when the host has no separate C++ driver; the pinned
AirPlay sources are C. CI or a release builder can override this explicitly
with `LIBREECHO_AIRPLAY_CXX`.

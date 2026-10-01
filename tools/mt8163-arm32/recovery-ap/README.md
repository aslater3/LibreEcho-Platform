# LibreEcho recovery-AP packaging and probes (issue #96, Platform side)

This directory owns the Platform half of the secure recovery access point:

- the pinned, statically-built AP dependencies and their sources/licenses;
- the host builder that verifies those pins and produces the ARM32 binaries;
- the compiled physical button detector and its host fixture;
- the module README and pin documentation.

The runtime probes ship in the initramfs overlay (`../initramfs/`) because they
are installed as part of the recovery image. The compiled components
(`hostapd`, `dnsmasq`, `iw` and the button detector) are **not** overlay files:
they are staged by `build_recovery_image.py
--recovery-ap-binaries/--recovery-ap-metadata` as hash-pinned ARM32 ELF members
and verified by `verify_recovery_image.py`.

| Overlay source | Image path | Role |
| --- | --- | --- |
| `libreecho-recovery-ap-probe` | `/usr/local/sbin/libreecho-recovery-ap-probe` | strict nl80211 AP-capability probe (`--recovery-ap-probe`) |
| `libreecho-recovery-ap-ready` | `/usr/local/sbin/libreecho-recovery-ap-ready` | readiness probe delegate (`--recovery-ready-probe`) |
| `libreecho-recovery-net-up` | `/usr/local/sbin/libreecho-recovery-net-up` | STA-release + portal address bring-up |
| `libreecho-recovery-net-down` | `/usr/local/sbin/libreecho-recovery-net-down` | exact address/ownership teardown |

| Pinned component | Image path | Role |
| --- | --- | --- |
| `hostapd` | `/usr/local/sbin/hostapd` | WPA2-PSK captive AP |
| `dnsmasq` | `/usr/local/sbin/dnsmasq` | DHCP/DNS for the portal |
| `iw` | `/usr/local/sbin/iw` | nl80211 probe transport |
| `libreecho-recovery-button` | `/usr/local/sbin/libreecho-recovery-button` | compiled evdev action-button (~5 s) detector; writes the tmpfs marker |

Host fixtures and contract tests live in `../test_recovery_ap.py` and are run by
the `Button backport checks` workflow.

## Detector contract

`libreecho-recovery-button` is a **compiled** evdev reader built by
`build_recovery_ap.sh` from `libreecho-recovery-button.c`.  It:

- opens its evdev node `O_RDONLY|O_NONBLOCK`, asks the kernel for
  `CLOCK_MONOTONIC` records, and reads the current key bitmap once with
  `EVIOCGKEY`, so a button **already held before the helper opens the node** is
  detected even though it produces no further key events (a shell helper cannot
  issue that ioctl, which is why the detector is compiled);
- measures the hold with `CLOCK_MONOTONIC` elapsed time from observation, not
  from a source-supplied event timestamp, so a crafted fixture cannot arm the
  marker by claiming a large timeval delta;
- arms `/run/libreecho/recovery-mode` — a root-owned, mode-0600 file on the boot
  tmpfs — with the content networkd validates:

```
libreecho-recovery-v1
hold_ms=5000
```

It is bounded end to end: with no held button it returns after its short startup
no-key window, and every path is capped by an absolute maximum, so a boot
without the button cannot stall.  It never reboots, never writes a persistent
filesystem and never touches a partition.  The marker path is guarded to a
`/run/` component, so the helper cannot be pointed at persistent storage.

The already-held branch is proven on the host by linking the real detector with
a strong `le_button_probe_initial_state` override
(`test_recovery_button_eviocgkey.c`), the EVIOCGKEY weak fixture; the compiled
detector's other branches are proven against native `struct input_event`
fixtures.

## AP probe contract

`libreecho-recovery-ap-probe [supported|ready]` (default `supported`):

- `supported` — the interface exists and its phy advertises `AP` in its nl80211
  supported interface modes (`iw phy ... info`).  "The binary exists" is never
  accepted as proof.
- `ready` — the AP is actually serving: the interface is currently in AP mode,
  the hostapd control surface exists, a live DHCP/DNS incarnation owns the
  daemon pidfile, and the interface carries the AP address.  This is the
  readiness check for networkd's `--recovery-ready-probe`.

Both fail closed with a bounded `unavailable: <reason>` line.

## Interface handover

`libreecho-recovery-net-up --interface IFACE [--address ADDR/PREFIX]` releases
the client STA plane through its owning service (pidfile-scoped, never
`pkill`), records exactly the interface/address it took in
`/run/libreecho/recovery-net.state`, brings the link up and assigns the portal
address.  A failure rolls the client service back.  `libreecho-recovery-net-down
--interface IFACE` removes **only** the address it recorded and restores the
client service only when it was the one that stopped it; with no recorded
ownership it is a successful no-op.  Neither helper uses `/dev/wmtWifi`, a
pattern-based kill, or a reboot.

## Pinned dependencies

`SOURCE.lock` pins each component's upstream URL, SHA-256, licence and the
licence file that must exist inside the upstream source tree.  The hashes were
computed from the real archives at those URLs.  During a build the script
verifies each archive hash, extracts it, and refuses to continue if a declared
licence text is missing from the tree.

Build the static ARM32 binaries with an external toolchain:

```sh
tools/mt8163-arm32/recovery-ap/build_recovery_ap.sh --verify \
  --cache /path/to/downloaded/archives \
  --libnl-archive /path/to/libnl-3.11.0.tar.gz
tools/mt8163-arm32/recovery-ap/build_recovery_ap.sh --build \
  --cache /path/to/downloaded/archives \
  --libnl-archive /path/to/libnl-3.11.0.tar.gz \
  --output /path/to/out --cc arm-linux-musleabihf-gcc \
  --ar arm-linux-musleabihf-ar --ranlib arm-linux-musleabihf-ranlib \
  --strip arm-linux-musleabihf-strip
```

`--verify` fails closed on a missing archive, a non-regular archive, a
malformed or mismatched SHA-256, or a GPL component with no recorded
corresponding-source offer; `--build` performs the same verification first and
refuses to compile against anything unverified.  `iw` and `hostapd`
(`CONFIG_LIBNL32`) reuse the libnl already pinned for wpa_supplicant; `hostapd`
is built with the internal crypto/TLS backend, and the builder refuses a config
that still enables DPP/SAE/OWE/FILS/PASN (the internal backend cannot link
`crypto_ec_*`).

Both modes end by emitting `recovery-ap-binaries.json` into the output
directory.  That document is the *only* accepted input for
`build_recovery_image.py --recovery-ap-metadata`: it records, per shipped
binary, the SHA-256 and size of the artefact the builder just produced plus the
licence, https source, pinned source SHA-256 and on-image licence copies that
ship with it.  It is generated from the real artefacts, never hand-written, so
the metadata and the binaries can never drift apart.  The image builder
re-validates every field and refuses a bundle whose metadata lacks provenance or
a corresponding-source offer; the image stages the document at
`/etc/libreecho/recovery-ap-binaries.json` so the attribution travels with the
binaries.  `--emit-metadata` re-emits the document for an existing output
directory (used by CI to validate the build result) without re-verifying the
source archives.

## Licence provenance and GPL source offer

`SOURCE.lock` records each component's licence, but a pinned archive is not
enough on its own: the builder verifies that the declared licence text is
actually present in the verified source tree, and the `source_offer` block
records the corresponding-source obligation for GPL components.  The recovery
image redistributes a compiled `dnsmasq` (GPL-2.0-or-3.0), so the pinned
upstream archive named in `source_offer` *is* the complete corresponding
source; the builder refuses to run when a GPL component is pinned without that
offer, so a GPL binary can never ship without its source obligation recorded.
`hostapd` (BSD-3-Clause), `iw` (ISC) and the static, not-staged `libnl`
(LGPL-2.1-only) build input are recorded the same way.

The `on_image` block maps each redistributed binary to the verbatim licence
copies that ship in the image's `libreecho-core` licence bundle
(`hostapd-2.10-COPYING.txt`, `dnsmasq-2.90-COPYING.txt` + `-v3.txt`,
`iw-5.19-COPYING.txt`); the emitted metadata carries those names, and the
verifier fails an image whose metadata points at a licence copy that is not
staged.  `THIRD_PARTY_NOTICES.md` documents the same components and the GPL
source offer for on-device readers.  The pinned-source → binary →
licence/source chain therefore closes in the packaged contract, not only in
this README.

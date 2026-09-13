# LibreEcho core runtime — third-party notices

LibreEcho is a mixed-license collective work. Every component remains under its
own license; this notice does not relicense third-party code or data.

The exact source commits, binary hashes, build inputs, and source-offer URLs are
recorded in `COMPONENTS.json`, the image manifest, and the release SPDX SBOM.

## Core operating system

- **Linux 6.1 and MT8163 product drivers** — GPL-2.0-only. Exact corresponding
  source: https://github.com/aslater3/LibreEcho-Linux-6.1
- **LibreEcho Platform/initramfs tooling** — GPL-2.0-only. Exact corresponding
  source: https://github.com/aslater3/LibreEcho-Platform
- **LibreEcho UI/services** — MIT. Exact source:
  https://github.com/aslater3/LibreEcho-UI
- **AOSP adbd** — Apache-2.0, built from the exact AOSP commit recorded in the
  image manifest. The AOSP NOTICE and Apache license accompany this bundle.

## Runtime utilities

- **BusyBox 1.37.0** — GPL-2.0-only. The release rebuilds the binary from
  the pinned upstream archive and the public
  `tools/mt8163-arm32/busybox/busybox-1.37.0.config`; build metadata records the
  compiler, source/config hashes, and output hash.
- **musl 1.2.5** — MIT. The release rebuilds the ARM32 dynamic loader from the
  pinned upstream archive; build metadata records the compiler and output hash.
- **wpa_supplicant 2.10** — BSD-3-Clause. The release rebuilds a static
  WPA2-PSK client with nl80211 preferred and WEXT retained as a fallback, using
  internal crypto, the pinned upstream archive, and the public config. The
  binary prints the included BSD terms with `wpa_supplicant -L`.
- **libnl 3.11.0** — LGPL-2.1-only. Its pinned upstream source is rebuilt as
  static `libnl-3` and `libnl-genl-3` archives and linked into wpa_supplicant;
  the complete corresponding source and build instructions accompany releases.
- **LibreEcho MT8163 connectivity helpers** — GPL-2.0-only. All five ARM32
  helpers are rebuilt from the checked-in Platform sources; no extracted WMT
  userspace executable is shipped.
- **wireless-tools 30~pre9** — GPL-2.0-only for the utilities and
  LGPL-2.1-or-later for the incorporated `wireless.21.h` interface. The exact
  upstream archive, SHA-256, static build metadata, and complete `COPYING`
  record are emitted by the source builder and included with the image.
- **wireless-regdb 2025.10.07-0ubuntu1~24.04.1** — ISC. The pinned Ubuntu
  upstream archive contains the exact `regulatory.db` and signature shipped by
  the image; the materializer verifies both output hashes before packaging.
- **TinyALSA e43025bbf702eb7dd8edd48c1eb50530c60f1de8** — BSD-3-Clause.
- **libsodium 1.0.18** — ISC, statically linked into the OTA verifier.
- **BlueZ SBC codec** — LGPL-2.1-or-later. The Bluetooth A2DP-SINK profile
  service in `libreecho-btd` statically links the vendored BlueZ SBC library
  (`sbc`, upstream `b3deb8a5dcfb42d8c10ba1f2f1ac9bd7bf7271cc`). The complete
  corresponding source ships with the LibreEcho UI source offer; the
  LGPL-2.1 text accompanies this bundle. Relinking instructions are in the
  UI `Makefile`.

## Compiler/runtime closure

Some statically linked executables contain GNU C Library and GCC runtime code.
The release source offer records the exact toolchain, glibc source under
LGPL-2.1-or-later, GCC runtime source under GPL-3.0-or-later WITH
GCC-exception-3.1, and LibreEcho source/build instructions sufficient to relink.

## MT8163 audio FPGA bridge — included, release-blocked

The audio-capable candidate includes `i2s_to_spi_v34.bin` in the kernel firmware
source tree and embeds it through `CONFIG_EXTRA_FIRMWARE`. It is required by the
Radar-Puffin speaker and microphone FPGA path. Its 30,964-byte SHA-256 is
`77a558bacdaaf9e343f02f2d74f27a5f2bb2dc8b6d66cc2499b60ed14ef62fe6`.

The binary remains **blocked from public redistribution** until authoritative
creator/generation provenance, license or source-offer terms, and redistribution
permission are established. Its presence in the source tree proves the exact
candidate can be reproduced; it does not by itself grant redistribution rights.

## Owner-device connectivity firmware

No MT8163 vendor connectivity firmware is included in this release. The running
device imports required files locally and read-only from the owner's
`system_a`; those files are never uploaded or redistributed by LibreEcho.

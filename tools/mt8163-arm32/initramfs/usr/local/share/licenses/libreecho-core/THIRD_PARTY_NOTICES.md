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
- **Mbed TLS 3.6.4** — Apache-2.0 (dual-licensed Apache-2.0 OR
  GPL-2.0-or-later; LibreEcho uses the Apache-2.0 option). The release rebuilds
  static ARM32 `libmbedtls`, `libmbedx509`, and `libmbedcrypto` archives from
  the pinned upstream release archive and links them into `libreecho-web` and
  `libreecho-radiod` for HTTPS. The source-archive SHA-256, build requirements,
  and produced archive hashes are recorded in
  `tools/mt8163-arm32/mbedtls/SOURCE.lock` and `mbedtls-3.6.4-NOTICE.txt`.
  No dynamic Mbed TLS library is shipped.
- **libogg 1.3.5 / libopus 1.4 / libopusfile 0.12** — BSD-3-Clause (Xiph.Org).
  The release rebuilds the pinned static, HTTP/TLS-free ARM32 Opus decode stack
  from the upstream release archives and links it into `libreecho-radiod` for
  Ogg Opus radio playback. `libopusfile` is compiled from its four local-file
  sources only, so the `op_open_url`/`op_http_*` API and its libcurl/OpenSSL
  dependencies are absent by construction. The source-archive SHA-256 values,
  build contract, and produced archive hashes are recorded in
  `tools/mt8163-arm32/ui/opus/SOURCE.lock`; the verbatim licences accompany this
  bundle as `libogg-1.3.5-COPYING.txt`, `opus-1.4-COPYING.txt`, and
  `opusfile-0.12-COPYING.txt`, and are also installed into the built prefix's
  `licenses/` directory.

## Recovery access point

The recovery access point redistributes compiled, statically linked third-party
binaries. Their pins, SHA-256 values, licences and corresponding-source offer
are recorded in `tools/mt8163-arm32/recovery-ap/SOURCE.lock`; the builder
(`build_recovery_ap.sh`) verifies each pinned archive, the licence text inside
each extracted source tree, and the GPL offer before it compiles, and emits
`recovery-ap-binaries.json` binding each shipped binary to its hash, licence and
source. The image stages that metadata at
`/etc/libreecho/recovery-ap-binaries.json`.

- **hostapd 2.10** — BSD-3-Clause. `tools/mt8163-arm32/recovery-ap/SOURCE.lock`
  records the pinned upstream archive
  `https://w1.fi/releases/hostapd-2.10.tar.gz`; the verbatim licence accompanies
  this bundle as `hostapd-2.10-COPYING.txt`.
- **dnsmasq 2.90** — GPL-2.0-or-3.0. The complete corresponding source for the
  redistributed `dnsmasq` binary is the pinned upstream archive
  `https://thekelleys.org.uk/dnsmasq/dnsmasq-2.90.tar.xz`
  (SHA-256 `8e50309bd837bfec9649a812e066c09b6988b73d749b7d293c06c57d46a109e4`),
  as recorded in the `source_offer` block of that SOURCE.lock; the builder
  refuses to build a GPL component without it. The verbatim licences accompany
  this bundle as `dnsmasq-2.90-COPYING.txt` (GPL-2.0) and
  `dnsmasq-2.90-COPYING-v3.txt` (GPL-3.0).
- **iw 5.19** — ISC. The pinned upstream archive
  `https://mirrors.edge.kernel.org/pub/software/network/iw/iw-5.19.tar.gz`; the
  verbatim licence accompanies this bundle as `iw-5.19-COPYING.txt`.
- **libreecho-recovery-button** — GPL-2.0-only. The first-party compiled evdev
  action-button detector is built from
  `tools/mt8163-arm32/recovery-ap/libreecho-recovery-button.c` in this
  repository; its corresponding source is the LibreEcho-Platform repository.

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

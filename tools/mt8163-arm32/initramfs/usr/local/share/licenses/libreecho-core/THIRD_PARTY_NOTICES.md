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
- **wpa_supplicant 2.10** — BSD-3-Clause. The release rebuilds a static,
  WEXT-only WPA2-PSK client with internal crypto from the pinned upstream
  archive and public config, eliminating the previous libnl/glibc ambiguity.
  The binary prints the included BSD terms with `wpa_supplicant -L`.
- **LibreEcho MT8163 connectivity helpers** — GPL-2.0-only. All five ARM32
  helpers are rebuilt from the checked-in Platform sources; no extracted WMT
  userspace executable is shipped.
- **wireless-tools 30~pre9-16.1ubuntu2** — GPL-2.0-only.
- **wireless-regdb 2025.10.07-0ubuntu1~24.04.1** — ISC.
- **TinyALSA e43025bbf702eb7dd8edd48c1eb50530c60f1de8** — BSD-3-Clause.
- **libsodium 1.0.18** — ISC, statically linked into the OTA verifier.

## Compiler/runtime closure

Some statically linked executables contain GNU C Library and GCC runtime code.
The release source offer records the exact toolchain, glibc source under
LGPL-2.1-or-later, GCC runtime source under GPL-3.0-or-later WITH
GCC-exception-3.1, and LibreEcho source/build instructions sufficient to relink.

## MT8163 audio FPGA bridge — excluded

The public base kernel deliberately disables the Radar-Puffin machine driver
and FPGA-backed capture path. `i2s_to_spi_v34.bin` is not included in the public
source branch, boot image, OTA archive, or source offer because no authoritative
redistribution permission or FPGA source/generation record has been established.
The public base therefore makes no speaker or microphone claim.

## Owner-device connectivity firmware

No MT8163 vendor connectivity firmware is included in this release. The running
device imports required files locally and read-only from the owner's
`system_a`; those files are never uploaded or redistributed by LibreEcho.

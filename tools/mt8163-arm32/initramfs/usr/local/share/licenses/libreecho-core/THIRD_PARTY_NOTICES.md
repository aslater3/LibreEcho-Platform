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

- **BusyBox 1.37.0-r30 (Alpine)** — GPL-2.0-only. The complete configuration
  exported by the shipped binary, Alpine APKBUILD, Alpine packaging commit,
  patches, and upstream source are part of the corresponding-source offer.
- **musl 1.2.5-r21 (Alpine)** — MIT. The shipped loader hash is pinned in
  `COMPONENTS.json`.
- **wpa_supplicant 2.10** — BSD-3-Clause. The exact binary prints the included
  BSD terms with `wpa_supplicant -L`.
- **wireless-tools 30~pre9-16.1ubuntu2** — GPL-2.0-only.
- **wireless-regdb 2025.10.07-0ubuntu1~24.04.1** — ISC.
- **TinyALSA e43025bbf702eb7dd8edd48c1eb50530c60f1de8** — BSD-3-Clause.
- **libsodium 1.0.18** — ISC, statically linked into the OTA verifier.

## Compiler/runtime closure

Some statically linked executables contain GNU C Library and GCC runtime code.
The release source offer records the exact toolchain, glibc source under
LGPL-2.1-or-later, GCC runtime source under GPL-3.0-or-later WITH
GCC-exception-3.1, and LibreEcho source/build instructions sufficient to relink.

## MT8163 audio FPGA bridge

The kernel embeds `firmware/i2s_to_spi_v34.bin` (30,964 bytes, SHA-256
`77a558bacdaaf9e343f02f2d74f27a5f2bb2dc8b6d66cc2499b60ed14ef62fe6`).
The identical file appears in the published Amazon-device Linux 3.18 kernel
source lineage at commit `5b48c78b249ed9129fe92d30087de25b20152538`, distributed with the kernel's
GPL-2.0 COPYING file. Credit belongs to the Amazon/MediaTek device-kernel
contributors. LibreEcho preserves the file byte-for-byte and does not claim
original authorship.

## Owner-device connectivity firmware

No MT8163 vendor connectivity firmware is included in this release. The running
device imports required files locally and read-only from the owner's
`system_a`; those files are never uploaded or redistributed by LibreEcho.

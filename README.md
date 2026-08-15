# LibreEcho Platform

`LibreEcho-Platform` is the public ARM32 product-tooling repository for the
supported LibreEcho target: the Amazon Echo Gen 2 / MT8163 Radar Puffin
platform. It is not the current Linux kernel source tree and it is not a
complete product release by itself.

## What this repository owns

This repository contains the public, release-relative pieces that make the
platform image reproducible and reviewable:

- ARM32 recovery-image and boot-envelope tooling;
- initramfs startup, update, rollback, and boot-control contracts;
- service and feature-payload packaging for the public product profiles;
- OTA bundle creation, signature verification, and release-source checks;
- owner-local vendor-firmware import validation;
- source locks, third-party notices, artifact verifiers, and focused host tests;
- historical compatibility material retained while the platform boundary moves.

The public source is deliberately separate from private release orchestration.
Private build roots, signing inputs, deployment evidence, and owner-local
artifacts are not part of this repository and must never be copied into it.

## Repository boundaries

The current ownership map is:

| Area | Repository | Scope |
| --- | --- | --- |
| Platform tooling and ARM32 image contracts | `LibreEcho-Platform` | This repository: initramfs, payload packaging, OTA, recovery tooling |
| Current kernel and device tree | [`LibreEcho-Linux-6.1`](https://github.com/aslater3/LibreEcho-Linux-6.1) | Linux 6.1 drivers, DT/DTS, kernel configuration, and kernel provenance |
| UI and service daemons | [`LibreEcho-UI`](https://github.com/aslater3/LibreEcho-UI) | Web UI, APIs, product daemons, and UI-side runtime behavior |
| Product coordination and release documentation | [`LibreEcho`](https://github.com/aslater3/LibreEcho) | Product status, release notes, public issue tracking, and cross-repository policy |
| Public installation and operator documentation | [`LibreEcho-Docs`](https://github.com/aslater3/LibreEcho-Docs) | Installation, recovery, supported hardware, privacy, and release guidance |
| Private release orchestration | `LibreEcho-Build` | Maintainer-only pipeline inputs and deployment/signing coordination; not public |

The current Linux ownership boundary is **Linux 6.1**. The historical Linux 3.18
source retained in this repository is compatibility material only; it is not the
source of truth for current kernel, device-tree, or driver changes. Kernel fixes
belong in `LibreEcho-Linux-6.1`, while initramfs and product-tooling fixes belong
here.

## Supported target and non-goals

The maintained target is the supported Echo Gen 2 MT8163 Radar Puffin hardware
revision described by the release contract. The image profile, artifact
inventory, partition geometry, and release channel are verified rather than
inferred from arbitrary device paths.

This repository does **not** claim to support:

- other Echo generations or unverified hardware revisions;
- arbitrary partition layouts, boot chains, or vendor firmware;
- a generic Linux distribution or a general-purpose recovery environment;
- private build/deployment roots, mutable `CURRENT` pointers, or maintainer-only
  evidence directories;
- redistribution of connectivity firmware, calibration data, boot-chain data,
  credentials, or device identity material;
- hardware validation merely because a host build or static verifier passes.

## Owner-local firmware boundary

Four connectivity-firmware inputs are imported only from the owner's device by
the reviewed vendor-import path. The importer checks the expected source paths,
file types, sizes, hashes, and target inventory before staging them. These files,
calibration data, vendor boot-chain material, MAC addresses, and identity data
remain local to the owner's device.

They must not be uploaded, cached in CI, committed to source control, included in
public release assets, or recorded in logs/evidence. A public build must fail
closed when required owner-local inputs are missing or do not match the reviewed
contract. See [`tools/mt8163-arm32/initramfs/vendor-assets/README.md`](tools/mt8163-arm32/initramfs/vendor-assets/README.md)
and [`docs/repository-boundary.md`](docs/repository-boundary.md).

## Safe host checks

These commands exercise public source contracts and do not flash hardware,
write device partitions, or require private deployment roots:

```sh
python tools/mt8163-arm32/test_recovery_image_tools.py
python tools/mt8163-arm32/test_source_offer_tools.py
python -m py_compile \
  tools/mt8163-arm32/build_recovery_image.py \
  tools/mt8163-arm32/verify_recovery_image.py \
  tools/mt8163-arm32/ota/make_ota_bundle.py
```

For pipeline contract tests, set `LIBREECHO_PIPELINE_ROOT` to a reviewed,
release-relative pipeline checkout. Do not substitute a private path in source,
documentation, or a UI-controlled value. Shell syntax checks can be run with
`bash -n` over the tracked `tools/mt8163-arm32` scripts.

A host check is not hardware acceptance. A complete image still requires the
paired current kernel, UI bundle, private build orchestration, independent image
verification, and separately recorded runtime/hardware evidence.

## Release, licensing, and source closure

The signed OTA contract and release workflow are documented in
[`tools/mt8163-arm32/ota/README.md`](tools/mt8163-arm32/ota/README.md). Component
licenses and notices are kept beside the component or in
[`tools/mt8163-arm32/third-party-licenses/`](tools/mt8163-arm32/third-party-licenses/).
The public source/component inventory is embedded in the image license closure;
product-level release and corresponding-source records belong in
[`LibreEcho`](https://github.com/aslater3/LibreEcho) and
[`LibreEcho-Docs`](https://github.com/aslater3/LibreEcho-Docs).

A release must be reproducible from its pinned public source and explicitly
identified owner-local inputs. Signing keys, private repository tokens,
credentials, deployment pointers, and unpublished build/evidence artifacts are
never required in this repository or in a public issue report.

## Historical source boundary

The original upstream Linux 3.x root release notes are preserved at
[`docs/historical/upstream-linux-3x-README`](docs/historical/upstream-linux-3x-README)
for provenance. Historical combined candidate trees and stale paths are obsolete;
do not use them as current build or installation instructions. The current
boundary summary is [`docs/repository-boundary.md`](docs/repository-boundary.md).

## Contributing and filing issues

Before opening an issue, run the relevant safe host checks and include the exact
public commit, command, sanitized output, target/release profile, and whether the
observation is source-only, image/build, or live hardware. Never include
passwords, private keys, Wi-Fi credentials, serials, MAC addresses, owner-local
firmware, private filesystem paths, or unpublished release artifacts.

- Platform tooling, initramfs, OTA, and recovery contracts: file the issue here.
- Kernel, DT/DTS, driver, and kernel-config behavior: use
  [`LibreEcho-Linux-6.1`](https://github.com/aslater3/LibreEcho-Linux-6.1).
- UI, API, and service-daemon behavior: use
  [`LibreEcho-UI`](https://github.com/aslater3/LibreEcho-UI).
- Product release gates and cross-repository policy: use
  [`LibreEcho`](https://github.com/aslater3/LibreEcho).
- Installation and operator-documentation defects: use
  [`LibreEcho-Docs`](https://github.com/aslater3/LibreEcho-Docs).

Destructive operations such as flashing, rebooting, partition writes, or live
installation are separate authorization and acceptance gates. Do not perform
them as an implicit part of a host-only issue fix.

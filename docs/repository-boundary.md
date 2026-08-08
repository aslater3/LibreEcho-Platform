# Repository boundary

`LibreEcho-Platform` is the ARM32 product-tooling repository. The local checkout
may retain the historical `LibreEcho-Kernel/` directory name during migration.
It contains the
initramfs, feature payload packaging, OTA verification, and release workflow.
Its historical Linux 3.18 tree remains for compatibility and is not the current
kernel source of truth.

The current kernel line is maintained separately in
[`LibreEcho-Linux-6.1`](https://github.com/aslater3/LibreEcho-Linux-6.1). Kernel,
device-tree, and driver changes belong there. The current PRD kernel baseline
is commit `2aaa8bfae1cc7c9aed5afe0fbe9a8e6abcbc6872`; follow-up fixes remain on
review branches until verified.

The UI and service daemons are maintained in
[`LibreEcho-UI`](https://github.com/aslater3/LibreEcho-UI). Product-level
release notes and cross-repository documentation belong in
[`LibreEcho`](https://github.com/aslater3/LibreEcho).

This boundary is source provenance only. A complete image still requires the
paired kernel, product tooling, UI bundle, and independent image verification.

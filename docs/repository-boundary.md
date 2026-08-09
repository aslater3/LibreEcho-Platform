# Repository boundary

`LibreEcho-Platform` is the ARM32 product-tooling repository. The local checkout
may retain the historical `LibreEcho-Kernel/` directory name during migration.
It contains the
initramfs, feature payload packaging, OTA verification, and release workflow.
Its historical Linux 3.18 tree remains for compatibility and is not the current
kernel source of truth.

The current kernel line is maintained separately in
[`LibreEcho-Linux-6.1`](https://github.com/aslater3/LibreEcho-Linux-6.1). Kernel,
device-tree, and driver changes belong there. Public `main` is the clean
development source line; complete hardware-validated image identities are
pinned separately by private release provenance. Follow-up fixes remain on
review branches until verified.

The UI and service daemons are maintained in
[`LibreEcho-UI`](https://github.com/aslater3/LibreEcho-UI). Product-level
release notes and cross-repository documentation belong in
[`LibreEcho`](https://github.com/aslater3/LibreEcho).

This boundary is source provenance only. A complete image still requires the
paired kernel, product tooling, UI bundle, private build orchestration,
independent image verification, and separate runtime acceptance.

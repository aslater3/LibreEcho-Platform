# LibreEcho recovery-AP packaging and probes (issue #96, Platform side)

This directory owns the Platform half of the secure recovery access point:

- the pinned, statically-built AP dependencies and their sources/licenses;
- the host builder that verifies those pins and produces the ARM32 binaries;
- the module README and pin documentation.

The runtime helpers themselves ship in the initramfs overlay
(`../initramfs/`) because they are installed as part of the recovery image:

| Overlay source | Image path | Role |
| --- | --- | --- |
| `libreecho-recovery-button` | `/usr/local/sbin/libreecho-recovery-button` | physical action-button (~5 s) detector; writes the tmpfs marker |
| `libreecho-recovery-ap-probe` | `/usr/local/sbin/libreecho-recovery-ap-probe` | strict nl80211 AP-capability probe (`--recovery-ap-probe`) |
| `libreecho-recovery-ap-ready` | `/usr/local/sbin/libreecho-recovery-ap-ready` | readiness probe delegate (`--recovery-ready-probe`) |

Host fixtures and contract tests live in `../test_recovery_ap.py` and are run by
the `Button backport checks` workflow.

## Detector contract

`libreecho-recovery-button` observes the run's evdev event stream and, on a
single continuous hold of the action button (KEY_HELP, `0x8a`) of at least the
threshold, writes `/run/libreecho/recovery-mode` — a root-owned, mode-0600 file
on the boot tmpfs — with the content networkd validates:

```
libreecho-recovery-v1
hold_ms=5000
```

It never reboots, never writes a persistent filesystem, and never touches a
partition.  The marker path is guarded to a `/run/` component, so the helper
cannot be pointed at persistent storage.

Known limitation (honest gap): the detector is event-stream based, matching the
kernel's monotonic input timestamps.  A button that is already held *before* the
helper opens its evdev node and that produces no further key events cannot be
reconstructed in a shell helper (that needs `EVIOCGKEY`, an ioctl).  The init
call is placed so the node is open as early as the control plane allows, and a
pressed-then-released or pressed-after-open hold is detected normally.  A
compiled ioctl helper is the follow-up if the fully-pre-boot hold must be
covered.

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

## Pinned dependencies

`SOURCE.lock` pins each component's upstream URL, SHA-256, and license.  The
hashes were computed from the real archives at those URLs.  Build the static
ARM32 binaries with an external toolchain:

```sh
tools/mt8163-arm32/recovery-ap/build_recovery_ap.sh --verify \
  --cache /path/to/downloaded/archives
tools/mt8163-arm32/recovery-ap/build_recovery_ap.sh --build \
  --cache /path/to/downloaded/archives \
  --output /path/to/out --cc arm-linux-gnueabihf-gcc \
  --ar arm-linux-gnueabihf-ar --ranlib arm-linux-gnueabihf-ranlib \
  --sysroot /path/to/sysroot --native-root /path/to/native
```

`--verify` fails closed on a missing archive, a non-regular archive, or any
SHA-256 mismatch; `--build` performs the same verification first and refuses to
compile against anything unverified.  `iw` reuses the libnl already pinned for
wpa_supplicant.

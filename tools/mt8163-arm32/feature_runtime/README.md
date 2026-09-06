# Runtime replacement capsules

This directory contains host-only tooling for building and verifying a narrow
runtime replacement capsule. It does **not** activate, mount, install, patch,
or otherwise integrate a capsule with the updater, initramfs, PID1, or shipped
runtime.

## Format and dependency boundary

The output format is real SquashFS 4 using lz4 compression (`squashfs-lz4`).
The builder invokes `mksquashfs` with one processor, uid/gid 0, timestamps 0,
normalized modes, no xattrs, no hardlinks, and no duplicate removal. The
verifier reads the resulting filesystem with `unsquashfs` and checks its
members and content hashes. There is no tar/zip/fake-SquashFS fallback: a host
without the required tool exits with a clear dependency error.

## Allowlist

Each feature has exactly one replacement target compiled into both tools:

| Feature | Target |
| --- | --- |
| `airplay2` | `usr/local/sbin/libreecho-audio-engine` |
| `tts` | `usr/local/sbin/libreecho-ttsd` |
| `wakeword` | `usr/local/sbin/libreecho-waked` |
| `stt` | `usr/local/sbin/libreecho-sttd` |
| `assistant` | `usr/local/sbin/libreecho-agentd` |

Both commands require an explicit trusted `--max-bytes` cap. It must be a
positive integer no larger than 67108864 bytes (64 MiB); there is no unlimited
default. The builder rejects replacement input and final SquashFS output over
the cap, records the cap in the canonical manifest, and the verifier requires
the same cap from its CLI before checking the manifest or payload.

The builder accepts only a regular, non-symlink replacement source for the
feature's target. It emits only the target and its required normalized parent
directories. The verifier rejects traversal, symlinks, special files,
duplicate members, unexpected members, metadata drift, manifest tampering, and
content/hash/size mismatches.

## Commands

From the repository root:

```sh
python tools/mt8163-arm32/feature_runtime/package_runtime.py \
  --feature-id assistant \
  --base-payload path/to/base.squashfs \
  --base-manifest path/to/base.json \
  --product-release radar-puffin-v0.14.0 \
  --source-commit 0123456789abcdef0123456789abcdef01234567 \
  --component libreecho-agentd \
  --component-version radar-puffin-v0.14.0 \
  --build-identity assistant-build-20260831 \
  --service-dependency libreecho-runtime-base \
  --compatibility '{"abi":"arm32-linux-gnueabihf-v1","model":"mt8163-radar-puffin","mounts":["/usr/local/sbin"],"dependencies":["libreecho-runtime-base"]}' \
  --replacement usr/local/sbin/libreecho-agentd=path/to/replacement \
  --max-bytes 1048576 \
  --output path/to/runtime-capsule.squashfs \
  --manifest path/to/runtime-capsule.json

python tools/mt8163-arm32/feature_runtime/verify_runtime.py \
  --feature-id assistant \
  --base-payload path/to/base.squashfs \
  --base-manifest path/to/base.json \
  --product-release radar-puffin-v0.14.0 \
  --source-commit 0123456789abcdef0123456789abcdef01234567 \
  --component libreecho-agentd \
  --component-version radar-puffin-v0.14.0 \
  --build-identity assistant-build-20260831 \
  --service-dependency libreecho-runtime-base \
  --compatibility '{"abi":"arm32-linux-gnueabihf-v1","model":"mt8163-radar-puffin","mounts":["/usr/local/sbin"],"dependencies":["libreecho-runtime-base"]}' \
  --max-bytes 1048576 \
  --capsule path/to/runtime-capsule.squashfs \
  --manifest path/to/runtime-capsule.json
```

The base manifest must use schema version 1 and format `squashfs-lz4`. The
capsule manifest uses schema version 1 and exactly records the feature,
component name, component version, immutable build identity, product release,
source commit, service dependencies, ABI/model/mount/dependency compatibility
constraints, base manifest/payload identity, capsule payload filename/hash/size,
and the allowlisted replacement's base hash, normal executable mode (`0755`),
content hash, and size, plus the explicit `max_bytes` cap. The verifier
requires the same contract values as explicit inputs, so changing a
boot-critical ABI, model, mount, or dependency
fails closed. All archive and manifest modes reject setuid, setgid, and sticky
bits. JSON output is canonicalized as sorted, indented JSON with a trailing
newline for reproducible manifests.

## Safe host test

```sh
python -m unittest -q tools.mt8163-arm32.feature_runtime.test_runtime_package
```

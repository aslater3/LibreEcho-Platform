# ARM32 TinyALSA audio tools

The release build does not consume checked-in executable files from this
directory. `build_audio_tools.sh` rebuilds static ARM32 hard-float utilities
from TinyALSA commit `e43025bbf702eb7dd8edd48c1eb50530c60f1de8`, whose
public archive and SHA-256 are pinned in `SOURCE.lock`.

The builder verifies and applies `tinyalsa-mt8163.patch` with zero fuzz. The
patch:

- explicitly calls `pcm_prepare()` before the first transfer;
- makes `tinyplay` return failure when a write fails;
- maps 24-bit `tinycap` capture to packed `PCM_FORMAT_S24_3LE`;
- makes `tinycap` return failure when no frames are captured; and
- includes `tinymix` for inspecting and enabling the Echo amplifier controls.

The build installs Linux ARM UAPI headers from the exact kernel source, uses the
pinned musl ARM32 cross compiler/sysroot, removes build IDs and volatile source
paths, rejects dynamic dependencies/interpreters, and writes
`tinyalsa-source.json` containing archive, patch, compiler, output hash, and
output-size records.

Reference reproducibility output from two independent build directories:

| file | size | SHA-256 |
| --- | ---: | --- |
| `tinyplay` | 385552 | `43f9ced6b4d43507bb8f5d30a104b78fa04f3623c8cbee332c3b47a8d173c27a` |
| `tinycap` | 366428 | `b89a3af2dd8f1dd0ee48b3e1903096ef3bceeced846bc18b7ff5b6f2138967f2` |
| `tinymix` | 267580 | `a1c8868cdd47033652ee2dfc4e25041d489147f215ca012f711d3c1c61e714ef` |

A release candidate records and independently verifies the exact hashes it
actually packages; these reference hashes are not a substitute for candidate
verification.

# Pinned static Opus decode stack for the radio Opus decoder

`build_opus.sh` builds the pinned static ARM32 Opus decode stack
(libogg + libopus + libopusfile) linked into LibreEcho-UI's `radiod` Opus
decoder (`src/adapter/radio_opus.c`). It lives beside the mbedTLS dependency
builder (`../mbedtls`) and follows the same contract: the image repository owns
the packaging, source acquisition stays outside this repository, and every step
fails closed on a hash, toolchain, output, or identity mismatch.

## Pinned sources

`opus/SOURCE.lock` records the upstream version, license, release URL, and
archive SHA-256 for each component. The builder never downloads: the caller
supplies the three archives and the builder verifies each byte before it
extracts anything.

| component | version | source | archive SHA-256 |
| --- | --- | --- | --- |
| libogg | 1.3.5 | `https://downloads.xiph.org/releases/ogg/libogg-1.3.5.tar.gz` | `0eb4b4b9…f4799b664` |
| opus | 1.4 | `https://downloads.xiph.org/releases/opus/opus-1.4.tar.gz` | `c9b32b42…e3ce49c51f` |
| opusfile | 0.12 | `https://github.com/xiph/opusfile/archive/refs/tags/v0.12.tar.gz` | `a20a1dff…cf00ef40` |

Each component's upstream `COPYING` is also pinned by hash. Reference copies
live in `opus/licenses/` for the repository inventory; the builder installs the
archive's own `COPYING` into the prefix `licenses/` directory under the name the
lock records (`license_copy`), so the shipped inventory cannot drift from the
source set.

## HTTP/TLS-free opusfile

The production decoder only opens local files. libopusfile is compiled directly
from its four local-file sources — `src/info.c`, `src/internal.c`,
`src/opusfile.c`, `src/stream.c` — and `src/http.c` is deliberately excluded, so
the archive carries no `op_open_url`/`op_http_*` API and no libcurl or OpenSSL
dependency. The builder fails closed if any HTTP/URL entry point appears in the
archive, and no configure/autotools step is needed for opusfile.

## Build

Explicit `--output` is mandatory; there is no default prefix.

```sh
tools/mt8163-arm32/ui/build_opus.sh \
  --ogg-archive /path/to/libogg-1.3.5.tar.gz \
  --opus-archive /path/to/opus-1.4.tar.gz \
  --opusfile-archive /path/to/opusfile-v0.12.tar.gz \
  --output /path/to/opus-prefix
```

For the ARM32 musl target, point `--cc` at the pinned cross compiler and pass
its triple as `--host`:

```sh
tools/mt8163-arm32/ui/build_opus.sh \
  --ogg-archive … --opus-archive … --opusfile-archive … \
  --output /path/to/opus-arm32-prefix \
  --cc /path/to/arm-linux-musleabihf-gcc \
  --host arm-linux-musleabihf
```

`--ar` (default derived from `--cc`) and `--jobs` (default
`LIBREECHO_BUILD_JOBS` or `2`) are optional.

The builder:

1. resolves the prefix identity (see below) and returns a cache hit immediately
   if a matching prefix already exists;
2. verifies the three archive SHA-256 values and each upstream `COPYING` hash;
3. probes what `--cc` actually emits and refuses a compiler whose objects are
   not ELF relocatables of that architecture, then requires every archive member
   to match;
4. builds libogg and libopus with their release `configure` scripts
   (`-Os`, static only) and compiles libopusfile directly;
5. verifies the required headers and decoder symbols exist, that the HTTP/URL
   symbols are absent, that no dynamic library was produced, and that no private
   build path leaked into the archives;
6. writes `opus-source.json` (license, source URLs and hashes, compiler, Python,
   the SHA-256 and size of each archive, a digest over the complete include
   tree) and `opus-identity.json` into the prefix.

## Identity cache

`opus-identity.json` is the cache key and records the compiler **target**, the
build **config**, and the three pinned source **hashes**. If `--output` already
exists, the builder compares that record with the request:

- **match** — `opus_identity_cache=hit` and nothing is rebuilt;
- **mismatch or missing record** — the build is refused (fail closed).

A prefix built for a different architecture, configuration, or source set can
never be silently reused as another build's output.

## Consuming the prefix

The prefix layout matches the upstream install convention:

```text
include/ogg/{ogg.h,os_types.h,config_types.h}
include/opus/{opus.h,opus_multistream.h,opus_types.h,opus_defines.h,
              opus_projection.h,opus_custom.h,opusfile.h}
lib/{libogg.a,libopus.a,libopusfile.a}
licenses/<component-COPYING.txt>
opus-identity.json
opus-source.json
```

Consumers add `-I<prefix>/include/opus -I<prefix>/include` and link the three
archives in dependency order (`libopusfile.a libopus.a libogg.a`) with `-lm`.
`opus/test_decode_host.c` is a host integration test that encodes, muxes and
decodes a real Ogg Opus stream through the built prefix (see the header of that
file for the exact compile line). The image-side integration hook is documented
in `evidence/opus-platform-handoff.md`.

## Verification

`test_build_opus.py` covers the bash syntax, the lock pins, the committed
licenses, the mandatory `--output`, the no-fetch guarantee, and the refusal paths
(wrong source hash, missing archive, missing/incorrect identity). A full host
build + decode round trip runs when the pinned archives are supplied:

```sh
LIBREECHO_OPUS_TEST_ARCHIVES=/path/to/archives \
  python3 tools/mt8163-arm32/ui/test_build_opus.py
```

## Licensing

libogg, libopus and libopusfile are all BSD-3-Clause (Xiph.Org). The verbatim
licenses are in `opus/licenses/`, and the same files are installed into the
built prefix under `licenses/`. The image license inventory lives in
`initramfs/usr/local/share/licenses/libreecho-core/`.

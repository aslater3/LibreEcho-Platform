# Pinned ARM32 mbedTLS for the production UI bundle

`build_ui_bundle.sh` links the pinned static ARM32 mbedTLS dependency built by
this directory into `libreecho-web` and `libreecho-radiod`. Without it the UI
Makefile selects `src/tls_stub.c`, `LE_TLS_AVAILABLE` resolves to `0`, and the
shipped Web UI keeps advertising the HTTPS toggle while port 8443 can never
listen (LibreEcho-UI issue #250).

## Pinned source

`SOURCE.lock` records the upstream version, license, release-archive URL, and
archive SHA-256 (`3.6.4`,
`ec35b18a6c593cf98c3e30db8b98ff93e8940a8c4e690e66b41dfc011d678110`). This
repository never downloads source: the builder consumes the archive supplied by
the image build and fails closed on any hash mismatch.

## Build

```sh
tools/mt8163-arm32/mbedtls/build_mbedtls.sh \
  --archive /path/to/mbedtls-3.6.4.tar.bz2 \
  --output /path/to/mbedtls-arm32 \
  --cc /usr/bin/arm-linux-gnueabihf-gcc
```

The builder:

1. verifies the archive SHA-256 against `SOURCE.lock`;
2. verifies the pinned build requirements and the locked interpreter floor;
3. probes what `--cc` actually emits and refuses a compiler whose objects do not
   carry the float ABI named by the lock's `target` (a soft-float
   `arm-linux-gnueabi-gcc` would otherwise publish hard-float provenance);
4. builds only the static ARM32 libraries (`make -C library static`);
5. verifies every archive member is an ARM32 ELF object, that no dynamic
   library was produced, that the headers exist, that the compiled-in version
   string matches the lock, and that no private build path leaked into the
   archives;
6. writes `mbedtls-source.json` into the output prefix with the license, source
   URL, source-archive hash, compiler, Python, build requirements, the SHA-256
   of each produced archive, the SHA-256 of `build_info.h`, and a digest over
   the complete include tree, so the verifier can bind the headers a consumer
   compiles against to the build that produced them.

## Build requirements

The upstream release tarball ships generated PSA driver wrappers that are older
than the Jinja templates producing them, so the library Makefile regenerates
them. The exact versions are pinned in `SOURCE.lock` and verified by the
builder, which fails closed if either is missing or different:

| package | version | why |
| --- | --- | --- |
| jinja2 | 3.1.6 | PSA driver wrapper templates |
| jsonschema | 4.25.1 | PSA driver JSON specification validation |

`SOURCE.lock` also records the interpreter floor for those pins: the jsonschema
release declares `Requires-Python >=3.9`, and the builder rejects an older
interpreter before doing any work rather than failing later on a pin that cannot
be installed.

Install them for the interpreter that runs the build, for example:

```sh
python3 -m venv /path/to/mbedtls-build-venv
/path/to/mbedtls-build-venv/bin/pip install jinja2==3.1.6 jsonschema==4.25.1
tools/mt8163-arm32/mbedtls/build_mbedtls.sh \
  --archive /path/to/mbedtls-3.6.4.tar.bz2 \
  --output /path/to/mbedtls-arm32 \
  --cc /usr/bin/arm-linux-gnueabihf-gcc \
  --python /path/to/mbedtls-build-venv/bin/python
```

`--jobs` (default `LIBREECHO_BUILD_JOBS` or `2`) bounds parallelism.

## Consuming the prefix

`LIBREECHO_UI_MBEDTLS_ROOT` must name the produced prefix. Before compiling,
`build_ui_bundle.sh` verifies that prefix against `SOURCE.lock` and its
`mbedtls-source.json`, normalises it to an absolute path, exports
`CPPFLAGS=-I<prefix>/include`, and passes the three verified archives to
`WEB_TLS_LIBS` and `RADIOD_TLS_LIBS` by absolute filename. Callers cannot
substitute a library search path or alternate mbedTLS archives while retaining
the pinned provenance. `ui/verify_ui_tls.sh` then fails the build unless the
compiled and stripped, staged binaries contain the real TLS implementation and
remain static ARM32.

## Licensing

Mbed TLS is dual-licensed Apache-2.0 OR GPL-2.0-or-later; LibreEcho uses it
under Apache-2.0. The license inventory lives in
`initramfs/usr/local/share/licenses/libreecho-core/`.

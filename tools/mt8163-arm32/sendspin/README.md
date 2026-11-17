# Sendspin SDK/ARM32 feasibility — ARMHF toolchain consumer

`build_sendspin.sh` cross-builds the Sendspin adapter fixture using the
**Product reviewed, archive-backed ARMHF cross-toolchain prefix** and executes it
under the **reviewed mDNS runtime closure**. Compile-input provenance and the
runtime symbol closure are deliberately separate; neither is conflated with the
future daemon.

## Compile input (the ARMHF prefix)

The ARM lane consumes a prefix materialized and verified by the Product
materializer. Identity is proven **from the locked `.deb` bytes** — never from a
receipt, a version string, or a path name.

```
SENDSPIN_ARMHF_PREFIX      absolute, real (non-symlink) staged prefix  -> SYSROOT
SENDSPIN_ARMHF_ARCHIVE_DIR the locked .deb archive pool (the verifier needs the bytes)
SENDSPIN_PRODUCT_ROOT      Product repo root supplying
                           build/ci/armhf_toolchain.py and
                           build/inputs/armhf-cross-toolchain.lock.json
                           (or set SENDSPIN_ARMHF_TOOLCHAIN_MODULE / _LOCK explicitly)
```

Before any target compiler or binutils runs, the build executes:

```
python3 <product>/build/ci/armhf_toolchain.py verify \
    --lock <lock> --archives <pool> --prefix <prefix>
```

and only after an archive-backed `armhf_toolchain_verify=PASS` does it derive,
**without evaluating any `env` output**:

```
SYSROOT         = <prefix>
CROSS_PREFIX    = <prefix>/usr/bin/arm-linux-gnueabihf-
LD_LIBRARY_PATH = <prefix>/usr/lib/x86_64-linux-gnu   (armhf compiler support libs)
```

Target compilers/binutils are used via absolute just-verified paths, so an
inherited `GCC_EXEC_PREFIX`/`COMPILER_PATH` cannot redirect them.

### Fail-closed behaviour

* A missing prefix, archive dir, Product root/verifier, lock, or any missing
  executable target tool makes the ARM lane fail closed. There is **no `/usr`
  or `/mnt` fallback** and no implicit `CROSS_PREFIX` default.
* Caller `SYSROOT`/`CROSS_PREFIX` that conflict with the verified prefix are
  rejected before the verifier runs.
* The target lane is sanitized: `CPATH`, `C_INCLUDE_PATH`, `CPLUS_INCLUDE_PATH`,
  `LIBRARY_PATH`, `GCC_EXEC_PREFIX`, `COMPILER_PATH` are unset and
  `LD_LIBRARY_PATH` is set explicitly, so an inherited loader/search path cannot
  bypass the reviewed toolchain. The host lane keeps its ambient environment.
* The prefix path may contain ordinary spaces and a literal `$`: the generated
  `arm-toolchain.cmake` quotes every path value and supplies `--sysroot` through
  `CMAKE_SYSROOT`. Characters CMake cannot represent in a quoted `set()` value —
  `;`, `"`, `\`, a newline, or an expansion (`${...}`, `$<...>`, `$ENV{...}`,
  `$CACHE{...}`) — are rejected at the gate, before any target tool runs.
* A non-empty verify result that does not carry the exact PASS line refuses the
  prefix.

## Runtime closure (unchanged)

`SENDSPIN_MDNS_LOCK` + `SENDSPIN_MDNS_ARCHIVES_DIR` stage the pinned reviewed
runtime `.deb` closure. The fixture is enforced against
`NEEDED={libm.so.6,libc.so.6,ld-linux-armhf.so.3}` and loader
`/lib/ld-linux-armhf.so.3`, then executed under the exact reviewed loader
(`QEMU_ARM`). C++ runtime is statically bound so the dynamic closure is exactly
the reviewed glibc/loader.

## Lanes

* **Host lane** uses the host toolchain and always builds and runs.
* **ARM lane** requires the verified staged prefix; `SKIP_ARM=1` omits it, and
  the test runner then reports the ARM tests honestly as **not run** (a host-only
  run can never emit an ARM pass). `SENDSPIN_REQUIRE_ARM=1` forces fail-closed
  whenever no ARM pass was produced: it dominates both the host-only success and
  `SENDSPIN_ALLOW_UNPROVISIONED=1`, so an unprovisioned or partial run exits `2`
  instead of `0`.

## Tests

* `test_sendspin_toolchain_consumer.py` — fast ARMHF prefix consumer contract:
  the negative/rejection matrix plus a real-script bootstrap that uses marker
  stand-ins (negative paths only, never presented as a real compile).
* `test_sdk_build.py` — the full runner (`python3 test_sdk_build.py`) with the
  source, runtime and toolchain-consumer verifier suites, and the provisioned
  host/ARM integration tests. Each verifier suite is judged from a structured
  child result, not a prose tail: a skip is accepted only from the consumer's
  two source-backed classes (`ToolchainConsumerNegativeTests`,
  `ToolchainConsumerBootstrapTests`) while the host lane is unprovisioned, and
  any other skip, a zero-test run or a non-zero child exit fails. An
  unprovisioned run therefore exits `2` unless acknowledged with
  `SENDSPIN_ALLOW_UNPROVISIONED=1` (exit `0`, integration reported **not run**) —
  or unless `SENDSPIN_REQUIRE_ARM=1`, which always exits `2` without an ARM pass.

## Not claimed

The host amd64 glibc, loader and host tools remain **external and unpinned**
(`SOURCE.lock` `runtime_requirements.compile_sysroot.pinned` stays `false`). This
is a technical feasibility lane, not the production daemon and not a hermetic or
cross-host reproducibility claim.

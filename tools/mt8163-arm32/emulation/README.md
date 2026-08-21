# LibreEcho emulation runner

Run the built LibreEcho device software **with no hardware**, for development and
testing. The release `boot.img` ships statically-linked ARM daemons and the web
bundle, so they run under QEMU/user-mode inside a `linux/arm/v7` container.

Useful for: exercising the web UI + HTTP API, bringing up the assistant daemon
(`agentd`), and driving the [`LibreEcho-UI` e2e/integration tests](https://github.com/aslater3/LibreEcho-UI/tree/main/tests)
against a `BASE_URL` — all without a physical device.

> This tooling ships the **recipe only**. Supply your own release `boot.img`
> (and optional `assistant.squashfs`); no device binaries are committed here.

## Build

```sh
./build.sh /path/to/boot.img                       # UI + adapters
./build.sh /path/to/boot.img assistant.squashfs    # + voice assistant (agentd)
```

Requires Docker (with `linux/arm/v7` emulation, e.g. via `binfmt`/QEMU), `cpio`,
and — for the assistant feature — `unsquashfs`.

## Run

```sh
# linux backend (real daemon sockets; hardware adapters unavailable unless run)
docker run -d --name libreecho-emu --platform linux/arm/v7 --memory 485m -p 127.0.0.1:8080:8080 libreecho-emu:latest

# mock backend (simulated hardware for every adapter — best for UI tests)
docker run -d --platform linux/arm/v7 -p 127.0.0.1:8080:8080 \
  --entrypoint /bin/busybox libreecho-emu:latest sh /entrypoint-mock.sh
```

`--memory 485m` mirrors the device's RAM budget. On first run the instance is in
bootstrap mode (`user_count:0`); create the admin via
`POST /api/v1/auth/bootstrap {username,password,password_confirm}`
(with the `X-LibreEcho-CSRF` header from `GET /api/v1/config`).

## What runs, what doesn't

- **Runs:** `libreecho-web` (UI + API on :8080), `agentd` (if the assistant
  feature was merged), and any adapter you start by hand.
- **Simulated:** the `mock` backend supplies data for every hardware adapter.
- **Not emulated:** real audio/mic/LED/Bluetooth hardware. The `linux` backend
  reports those adapters unavailable unless their daemons run; the `mock` backend
  fills them in. OTA slot/flash flows need a full-system VM, not this container.

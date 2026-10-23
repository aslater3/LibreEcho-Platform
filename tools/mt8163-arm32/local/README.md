# Persistent local services

LibreEcho is an open platform. Your own services — an SSH server with your own
keys, a Music Assistant experiment, a Bluetooth helper, a diagnostic logger —
can live in `/data/libreecho/local`, which survives A/B OTA updates. Official
OTA images stay signed and unchanged; your files are never touched by them.

## Layout

```text
/data/libreecho/local/
  enabled                  switch: present => your services run
  services/<name>/run      executable; runs in the foreground and is restarted
  services/<name>/down     optional: present => this service is not run
  services/<name>/once     optional: run once per boot (a startup script)
  bin/, etc/, ...          anything else you need
  .boot-guard              written by LibreEcho (see below)
```

`<name>` is lowercase letters, digits, `.`, `_` or `-`, up to 32 characters.
At most 16 services run. Each `run` starts in its own directory, as root.

## Switching it on and off

Turn **Local services** on in the web UI, or over USB ADB:

```sh
mkdir -p /data/libreecho/local/services
touch /data/libreecho/local/enabled
```

The supervisor (`/usr/local/sbin/libreecho-local`) checks every 5 seconds, so
creating or removing `enabled`, or a service's `down` file, applies without a
restart. Removing `enabled` stops every service and everything it started.

## Behaviour

- Services start only after LibreEcho's own services are up and, after an
  update, only once the update has been confirmed healthy. A service can
  therefore never block boot or cause an update to roll back.
- They run at low CPU and I/O priority and are the out-of-memory killer's
  first choice.
- A service that exits is restarted after 1, 2, 4 … 60 seconds. After 10 exits
  in 10 minutes it is marked `failed` and left stopped until the next boot.
- A `once` service gets a single run per boot, limited to 5 minutes.
- Output goes to `/var/log/libreecho/local/<name>.log` (RAM, cleared on reboot),
  capped at 256 KiB with one previous copy in `<name>.log.1`.
- `/run/libreecho/local.status` lists each service and its state (`running`,
  `down`, `failed`, `exited:<code>`); `libreecho-local status` prints it.

## Getting out of trouble

If a service makes the device misbehave:

1. Switch **Local services** off in the web UI.
2. Over USB ADB: `rm /data/libreecho/local/enabled`.
3. Hold the Action button for about 5 seconds as the device starts (the same
   hold that opens the recovery access point): local services are skipped for
   that boot.
4. Automatic: if services were enabled on three boots in a row that did not
   stay up for 3 minutes, they are skipped (`skipped:boot-loop-guard`). Delete
   `/data/libreecho/local/.boot-guard`, or switch Local services off and on in
   the web UI (which clears it), and they run again from the next boot.

If `/data/libreecho/local` is a symlink or not a directory, nothing runs
(`skipped:path-unsafe`) and the device still starts normally.

## Example: your own SSH server

The image's SSH (Network › Local access) signs in with web accounts and never
as root. For a root shell with your own keys, run a static Dropbear yourself on
another port:

```text
/data/libreecho/local/bin/dropbear          static ARM32 build
/data/libreecho/local/etc/authorized_keys
/data/libreecho/local/services/dropbear/run
```

```sh
#!/bin/sh
mkdir -p /tmp/local-dropbear
exec /data/libreecho/local/bin/dropbear -F -E -p 2222 \
    -R -D /tmp/local-dropbear -A /data/libreecho/local/etc/authorized_keys
```

## Rolling back to 0.14

LibreEcho 0.14.0 does not know about `/data/libreecho/local` and treats it as an
unexpected directory: a device that falls back to a 0.14.0 slot with it present
starts without its web UI and network, reachable only over USB ADB. Remove the
directory (or move it aside) before installing 0.14.0, or use a 0.14.x release
that accepts it.

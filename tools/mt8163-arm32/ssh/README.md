# Deferred WebUI-backed SSH

The SSH bundle is opt-in at image build time. It contains the static ARM32
`dropbear`, `dropbearkey`, and `scp` binaries, the runtime supervisor, and
non-secret `/etc/passwd`/`/etc/group` scaffolding.

When the bundle is present, `libreecho-init` starts
`/etc/init.d/libreecho-ssh.init`. The supervisor remains in a waiting state until
`/data/libreecho/config/users` is a root-owned, private, non-empty database using
the WebUI format:

```text
username:sha256:salt:digest
```

Dropbear has a small reviewed authentication hook that validates that file on
each password attempt. The supervisor derives ephemeral non-root account entries
and private homes under `/tmp`; it never writes a persistent `/etc/shadow` or
stores another password database. Invalid, missing, empty, unreadable, or
case-colliding account state stops Dropbear and keeps production WebUI binding on
loopback. Add, password-change, and delete operations therefore affect new SSH
sessions without a service restart; reboot and explicit restart regenerate the
ephemeral state.

Host keys are generated at runtime below `/tmp/dropbear`. Public-key
authentication is disabled. Root is present only as non-authenticating system
scaffolding; the WebUI SSH policy assigns users UIDs starting at 1000 and never
permits root login.

The supervisor status file is `/run/libreecho/ssh.status` and exposes only
non-secret mode, source, privilege, account-state, and host-key policy fields.

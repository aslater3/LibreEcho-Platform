# le-hwsim: simulated LibreEcho hardware

`le-hwsim` stands in for the hardware daemons so the real LibreEcho software can
run without a device (Platform #62). It serves the daemons' line-JSON protocol
on the same socket names (`audio.sock`, `led.sock`, `network.sock`,
`bluetooth.sock`), keeps plausible state, and logs every request to JSONL so a
test can assert what the software asked the "hardware" to do. Python 3,
standard library only.

```sh
# serve on a directory, then bind-mount it onto /run/libreecho for the real daemons
./le-hwsim serve --run-dir /tmp/sim-run --log /tmp/events.jsonl --timeout 600
# play a scenario timeline and check its expectations (exit 0/1)
./le-hwsim run scenarios/network-drop.json --run-dir /tmp/sim-run --timeout 120
```

Unprivileged, without a container, a user namespace gives the daemons the exact
socket paths they expect:

```sh
bwrap --dev-bind / / --tmpfs /run --bind /tmp/sim-run /run/libreecho \
  libreecho-web --backend linux --config /tmp/web-config.json --web-root web --listen 127.0.0.1:8080
```

Scenario format: `steps` change adapter state at a time offset
(`{"at": 1, "set": {"network": {"signal": 8}}}`) or call a test hook
(`{"at": 2, "act": "name"}`); `expect` lists requests that must arrive
(`{"adapter": "audio", "cmd": "speak", "args": {...}, "within_s": 5}`).

`test_hwsim.py` checks protocol conformance and, with `LE_HWSIM_WEB_BIN` set to
a host build of LibreEcho-UI's `libreecho-web`, runs the real web daemon's
linux backend against the simulator.

Not yet included: the microphone codec FIFO for running the real micd/waked
(the multichannel scene renderer shared with LibreEcho-UI's voice-e2e), and an
`entrypoint-hwsim.sh` for the ARM emulation container.

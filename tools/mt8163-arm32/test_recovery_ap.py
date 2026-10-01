#!/usr/bin/env python3
"""Fail-closed host tests for the Platform recovery-AP helpers (issue #96).

These tests exercise the real, shipped helpers with full isolation:

* the physical detector is the compiled evdev reader.  It is built for the host
  with the native ``struct input_event`` layout (timeval on the host ABI, not a
  hardcoded 32-bit record) and driven against fixture files, so the hold is
  proven with real CLOCK_MONOTONIC elapsed time rather than a crafted timestamp;
* the already-held case is proven by linking the real detector with the strong
  ``le_button_probe_initial_state`` override from
  ``recovery-ap/test_recovery_button_eviocgkey.c`` (the EVIOCGKEY weak fixture);
* the AP probe runs against stub ``iw``/``ip`` oracles on an isolated PATH;
* the ``net-up``/``net-down`` interface helpers run against a stateful ``ip``
  oracle and a stub wpa service, proving exact address/ownership handoff;
* the dependency builder refuses an archive whose SHA-256 does not match its
  pin, a GPL component without a corresponding-source offer, and a half-shipped
  metadata inventory.

No live device, radio, network, sysfs path, or image build is touched.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
INITRAMFS = TOOLS_DIR / "initramfs"
RECOVERY_AP = TOOLS_DIR / "recovery-ap"

DETECTOR_SRC = RECOVERY_AP / "libreecho-recovery-button.c"
EVIOCGKEY_HARNESS = RECOVERY_AP / "test_recovery_button_eviocgkey.c"
PROBE = INITRAMFS / "libreecho-recovery-ap-probe"
READY = INITRAMFS / "libreecho-recovery-ap-ready"
NET_UP = INITRAMFS / "libreecho-recovery-net-up"
NET_DOWN = INITRAMFS / "libreecho-recovery-net-down"
LOCK = RECOVERY_AP / "SOURCE.lock"
BUILDER = RECOVERY_AP / "build_recovery_ap.sh"

CC = shutil.which("cc") or shutil.which("gcc") or ""

TAG = "libreecho-recovery-v1"
KEY_HELP = 138
KEY_MUTE = 113
EV_KEY = 1
EV_SYN = 0


# --- native evdev fixture -------------------------------------------------
# The detector is compiled for the host, so the fixture must use the host's
# struct input_event layout (which is 24 bytes on 64-bit hosts and 16 on 32-bit
# ARM).  Deriving it through ctypes keeps the test honest: the same native
# struct the detector reads.

class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class _InputEvent(ctypes.Structure):
    _fields_ = [
        ("time", _Timeval),
        ("type", ctypes.c_uint16),
        ("code", ctypes.c_uint16),
        ("value", ctypes.c_int32),
    ]


INPUT_EVENT_SIZE = ctypes.sizeof(_InputEvent)


def input_event(sec: int, usec: int, type_: int, code: int, value: int) -> bytes:
    """One native ``struct input_event`` record using the host ABI."""
    event = _InputEvent()
    event.time.tv_sec = sec
    event.time.tv_usec = usec
    event.type = type_
    event.code = code
    event.value = value
    return ctypes.string_at(ctypes.byref(event), INPUT_EVENT_SIZE)


def syn(sec: int, usec: int) -> bytes:
    return input_event(sec, usec, EV_SYN, 0, 0)


def write_script(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


@unittest.skipIf(not CC, "no C compiler available")
class ButtonDetectorTests(unittest.TestCase):
    """The compiled evdev detector, built natively for the host."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.build = tempfile.TemporaryDirectory()
        build = Path(cls.build.name)
        cls.detector = build / "libreecho-recovery-button"
        subprocess.run(
            [CC, "-O2", "-Wall", "-Wextra", "-o", str(cls.detector), str(DETECTOR_SRC)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        cls.harness = build / "test_recovery_button_eviocgkey"
        subprocess.run(
            [CC, "-DLE_BUTTON_NO_MAIN", "-O2", "-o", str(cls.harness),
             str(EVIOCGKEY_HARNESS), str(DETECTOR_SRC)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.build.cleanup()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.marker = self.root / "run" / "libreecho" / "recovery-mode"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_detector(self, events: bytes, *extra: str) -> tuple[subprocess.CompletedProcess, float]:
        device = self.root / "event0"
        device.write_bytes(events)
        args = [str(self.detector), "--device", str(device),
                "--marker", str(self.marker), *extra]
        start = time.monotonic()
        result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=60)
        return result, time.monotonic() - start

    def test_no_key_leaves_boot_untouched(self) -> None:
        result, elapsed = self.run_detector(
            b"", "--hold-ms", "5000", "--press-window-ms", "150", "--max-ms", "600")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())
        # Real monotonic bound: the helper actually waited its press window
        # rather than trusting a source-supplied timestamp.
        self.assertGreaterEqual(elapsed, 0.13)

    def test_held_press_arms_after_real_monotonic_hold(self) -> None:
        # The event timestamp is deliberately stale (sec=0).  A synthetic
        # timestamp must NOT be able to arm the marker: the detector has to
        # observe a real ~250 ms hold through CLOCK_MONOTONIC.
        stream = input_event(0, 0, EV_KEY, KEY_HELP, 1) + syn(0, 0)
        result, elapsed = self.run_detector(
            stream, "--hold-ms", "250", "--max-ms", "4000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.is_file())
        self.assertGreaterEqual(elapsed, 0.22)
        content = self.marker.read_text()
        self.assertTrue(content.startswith(TAG), content)
        self.assertIn("hold_ms=250", content)
        self.assertEqual(stat.S_IMODE(self.marker.stat().st_mode), 0o600)

    def test_autorepeat_is_still_held(self) -> None:
        stream = (input_event(0, 0, EV_KEY, KEY_HELP, 1) + syn(0, 0) +
                  input_event(0, 0, EV_KEY, KEY_HELP, 2) + syn(0, 0))
        result, elapsed = self.run_detector(
            stream, "--hold-ms", "250", "--max-ms", "4000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.is_file())
        self.assertGreaterEqual(elapsed, 0.22)

    def test_release_before_threshold_never_arms(self) -> None:
        stream = (input_event(0, 0, EV_KEY, KEY_HELP, 1) +
                  input_event(0, 0, EV_KEY, KEY_HELP, 0))
        result, elapsed = self.run_detector(
            stream, "--hold-ms", "5000", "--press-window-ms", "150", "--max-ms", "600")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertLess(elapsed, 1.0)

    def test_wrong_key_code_never_arms(self) -> None:
        stream = (input_event(0, 0, EV_KEY, KEY_MUTE, 1) +
                  input_event(0, 0, EV_KEY, KEY_MUTE, 0))
        result, _ = self.run_detector(
            stream, "--hold-ms", "5000", "--press-window-ms", "150", "--max-ms", "600")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())

    def test_non_tmpfs_marker_is_refused(self) -> None:
        device = self.root / "event0"
        device.write_bytes(input_event(0, 0, EV_KEY, KEY_HELP, 1))
        evil = self.root / "evil" / "recovery-mode"
        result = subprocess.run(
            [str(self.detector), "--device", str(device), "--marker", str(evil)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-tmpfs", result.stderr)
        self.assertFalse(evil.exists())

    def test_already_held_is_seen_through_eviocgkey_fixture(self) -> None:
        result = subprocess.run(
            [str(self.harness)], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already-held-armed=ok", result.stdout)

    def test_detector_source_never_reads_sysfs(self) -> None:
        self.assertNotIn("/sys", DETECTOR_SRC.read_text())


class ApProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self._servers: list[socket.socket] = []

    def tearDown(self) -> None:
        for server in self._servers:
            server.close()
        self.tmp.cleanup()

    def make_iw(self, *, dev_ok: bool, mode: str, ap_mode: bool) -> None:
        dev = (f'echo "Interface {mode}"\necho "\\twiphy 0"\necho "\\ttype {mode}"'
               if dev_ok else 'exit 1')
        modes = "AP" if ap_mode else "managed"
        body = f'''case "$1" in
  dev) {dev} ;;
  phy) printf '\\tSupported interface modes:\\n\\t\\t * IBSS\\n\\t\\t * {modes}\\n\\n' ;;
  *) exit 2 ;;
esac
'''
        write_script(self.bin, "iw", body)

    def make_ip(self, address: str) -> None:
        write_script(self.bin, "ip", f'printf "    inet {address}/24 scope global\\\\n"\n')

    def env(self, **overrides: str) -> dict:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env.get('PATH', '')}"
        env.update(overrides)
        return env

    def run_probe(self, entry: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(entry), *args], text=True,
            env=env if env is not None else self.env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

    def test_supported_probe_accepts_real_ap_advertisement(self) -> None:
        self.make_iw(dev_ok=True, mode="managed", ap_mode=True)
        result = self.run_probe(PROBE)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_supported_probe_rejects_missing_ap_mode(self) -> None:
        self.make_iw(dev_ok=True, mode="managed", ap_mode=False)
        result = self.run_probe(PROBE)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ap-mode-unsupported", result.stderr)

    def test_supported_probe_rejects_absent_interface(self) -> None:
        self.make_iw(dev_ok=False, mode="managed", ap_mode=True)
        result = self.run_probe(PROBE)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("interface-absent", result.stderr)

    def test_probe_fails_closed_without_iw(self) -> None:
        result = self.run_probe(PROBE, env=self.env(RECOVERY_AP_IW="/nonexistent/iw"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing-iw", result.stderr)

    def bind_control_socket(self, path: Path) -> None:
        """Publish a real AF_UNIX socket the readiness probe can require."""
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        self._servers.append(server)

    def readiness_env(self, *, dhcp_pid: Path | None,
                      hostapd_socket: bool = True) -> dict:
        ctrl = self.root / "hostapd"
        ctrl.mkdir(exist_ok=True)
        if hostapd_socket:
            # hostapd publishes its control socket as ctrl_interface/<ifname>;
            # a bound AF_UNIX socket is the surface the probe must require.
            self.bind_control_socket(ctrl / "wlan0")
        env = self.env(RECOVERY_AP_CTRL=str(ctrl))
        if dhcp_pid is not None:
            env["RECOVERY_AP_DHCP_PID"] = str(dhcp_pid)
        return env

    def test_readiness_requires_live_control_dhcp_and_address(self) -> None:
        self.make_iw(dev_ok=True, mode="AP", ap_mode=True)
        self.make_ip("192.168.4.1")
        dhcp = subprocess.Popen(["sleep", "30"])
        try:
            dhcp_pid = self.root / "recovery-dhcp.pid"
            dhcp_pid.write_text(f"{dhcp.pid}\n")
            env = self.readiness_env(dhcp_pid=dhcp_pid)
            self.assertEqual(self.run_probe(PROBE, "ready", env=env).returncode, 0)
        finally:
            dhcp.terminate()
            dhcp.wait()

    def test_readiness_fails_when_dhcp_incarnation_is_dead(self) -> None:
        self.make_iw(dev_ok=True, mode="AP", ap_mode=True)
        self.make_ip("192.168.4.1")
        dhcp_pid = self.root / "recovery-dhcp.pid"
        dhcp_pid.write_text("2147483\n")  # not a live pid
        result = self.run_probe(PROBE, "ready", env=self.readiness_env(dhcp_pid=dhcp_pid))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dhcp-not-running", result.stderr)

    def test_readiness_fails_without_hostapd_control(self) -> None:
        self.make_iw(dev_ok=True, mode="AP", ap_mode=True)
        self.make_ip("192.168.4.1")
        env = self.env(RECOVERY_AP_CTRL=str(self.root / "missing"))
        env["RECOVERY_AP_DHCP_PID"] = str(self.root / "pid")
        env["RECOVERY_AP_CTRL"] = str(self.root / "missing")
        (self.root / "pid").write_text("1\n")
        result = self.run_probe(PROBE, "ready", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hostapd-control-absent", result.stderr)

    def test_readiness_rejects_shared_run_dir_without_control_socket(self) -> None:
        # The control directory is the shared /run/libreecho tree that init
        # creates for the recovery marker and the net-up ownership state, so its
        # mere existence must NOT be accepted as proof hostapd is live.
        self.make_iw(dev_ok=True, mode="AP", ap_mode=True)
        self.make_ip("192.168.4.1")
        dhcp = subprocess.Popen(["sleep", "30"])
        try:
            dhcp_pid = self.root / "recovery-dhcp.pid"
            dhcp_pid.write_text(f"{dhcp.pid}\n")
            env = self.readiness_env(dhcp_pid=dhcp_pid, hostapd_socket=False)
            result = self.run_probe(PROBE, "ready", env=env)
        finally:
            dhcp.terminate()
            dhcp.wait()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hostapd-control-absent", result.stderr)

    def test_readiness_rejects_directory_in_place_of_control_socket(self) -> None:
        # A directory named like the socket is not a live control surface: only
        # a real AF_UNIX socket for the served interface counts.
        self.make_iw(dev_ok=True, mode="AP", ap_mode=True)
        self.make_ip("192.168.4.1")
        dhcp = subprocess.Popen(["sleep", "30"])
        try:
            dhcp_pid = self.root / "recovery-dhcp.pid"
            dhcp_pid.write_text(f"{dhcp.pid}\n")
            (self.root / "hostapd" / "wlan0").mkdir(parents=True)
            env = self.readiness_env(dhcp_pid=dhcp_pid, hostapd_socket=False)
            result = self.run_probe(PROBE, "ready", env=env)
        finally:
            dhcp.terminate()
            dhcp.wait()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hostapd-control-absent", result.stderr)

    def test_readiness_rejects_client_mode_interface(self) -> None:
        self.make_iw(dev_ok=True, mode="managed", ap_mode=True)
        self.make_ip("192.168.4.1")
        result = self.run_probe(PROBE, "ready", env=self.readiness_env(dhcp_pid=None))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("interface-not-ap", result.stderr)

    def test_readiness_rejects_missing_address(self) -> None:
        self.make_iw(dev_ok=True, mode="AP", ap_mode=True)
        self.make_ip("10.0.0.2")
        dhcp = subprocess.Popen(["sleep", "30"])
        try:
            dhcp_pid = self.root / "recovery-dhcp.pid"
            dhcp_pid.write_text(f"{dhcp.pid}\n")
            result = self.run_probe(PROBE, "ready", env=self.readiness_env(dhcp_pid=dhcp_pid))
        finally:
            dhcp.terminate()
            dhcp.wait()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("interface-address-missing", result.stderr)

    def test_ready_wrapper_delegates_to_probe(self) -> None:
        source = READY.read_text()
        self.assertIn("libreecho-recovery-ap-probe", source)
        self.assertIn("ready", source)

    def test_probe_never_reads_sysfs(self) -> None:
        self.assertNotIn("/sys", PROBE.read_text())


class NetHandoverTests(unittest.TestCase):
    """net-up/net-down over a stateful ``ip`` oracle and a stub wpa service."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "run" / "libreecho"
        self.state_dir.mkdir(parents=True)
        self.ip_log = self.root / "ip.log"
        self.ip_state = self.root / "ipstate"
        self.ip_state.mkdir()
        self.wpa_log = self.root / "wpa.log"
        self.oracle = self.root / "ip"
        self.oracle.write_text(f'''#!/bin/sh
printf '%s\\n' "$*" >> "{self.ip_log}"
if [ "$1" = addr ]; then
    case "$2" in
        show) [ -f "{self.ip_state}/addr" ] && printf '    inet %s scope global\\n' "$(cat "{self.ip_state}/addr")"; exit 0 ;;
        add) [ -f "{self.ip_state}/fail_add" ] && exit 1; printf '%s' "$3" > "{self.ip_state}/addr"; exit 0 ;;
        del) rm -f "{self.ip_state}/addr"; exit 0 ;;
    esac
elif [ "$1" = link ]; then
    [ -f "{self.ip_state}/fail_link" ] && exit 1
    exit 0
fi
exit 0
''')
        self.oracle.chmod(0o755)
        # A real client service behaves like a oneshot: the first ``stop``
        # releases it and any later ``stop`` fails (nothing is left to stop),
        # while ``start`` re-arms it so a restore is observable.  This is what
        # makes the retry-ownership regression reproducible.
        self.wpa = self.root / "libreecho-wifi"
        self.wpa.write_text(f'''#!/bin/sh
echo "$*" >> "{self.wpa_log}"
count_file="{self.root}/stopcount"
case "$1" in
    stop)
        n=$(cat "$count_file" 2>/dev/null || printf 0)
        n=$((n + 1))
        printf '%s' "$n" > "$count_file"
        [ "$n" -le 1 ] && exit 0
        exit 1 ;;
    start)
        rm -f "$count_file"
        if [ -f "{self.root}/start_fail" ]; then
            n=$(cat "{self.root}/start_fail")
            n=$((n - 1))
            if [ "$n" -le 0 ]; then rm -f "{self.root}/start_fail"; else printf '%s' "$n" > "{self.root}/start_fail"; fi
            exit 1
        fi
        exit 0 ;;
esac
exit 0
''')
        self.wpa.chmod(0o755)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def env(self) -> dict:
        env = dict(os.environ)
        env["RECOVERY_AP_IP"] = str(self.oracle)
        env["RECOVERY_AP_WPA_SERVICE"] = str(self.wpa)
        return env

    def run_net(self, helper: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", str(helper), *args], text=True,
                              env=self.env(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=60)

    def ip_log_text(self) -> str:
        return self.ip_log.read_text() if self.ip_log.exists() else ""

    def wpa_log_text(self) -> str:
        return self.wpa_log.read_text() if self.wpa_log.exists() else ""

    def up(self, *extra: str) -> subprocess.CompletedProcess:
        return self.run_net(NET_UP, "--interface", "wlan0",
                            "--address", "192.168.4.1/24",
                            "--state-dir", str(self.state_dir), *extra)

    def test_net_up_assigns_address_and_records_ownership(self) -> None:
        result = self.up()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("link set dev wlan0 up", self.ip_log_text())
        self.assertIn("addr add 192.168.4.1/24 dev wlan0", self.ip_log_text())
        self.assertIn("stop", self.wpa_log_text())
        state = (self.state_dir / "recovery-net.state").read_text()
        self.assertIn("iface=wlan0", state)
        self.assertIn("address=192.168.4.1/24", state)
        self.assertIn("stopped=1", state)

    def test_net_up_is_idempotent(self) -> None:
        self.assertEqual(self.up().returncode, 0)
        result = self.up()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already-configured", result.stdout)
        self.assertEqual(self.ip_log_text().count("addr add"), 1)

    def test_net_up_re_run_keeps_ownership_across_a_failed_second_stop(self) -> None:
        # First run releases the client plane and records stopped=1.
        self.assertEqual(self.up().returncode, 0)
        state = self.state_dir / "recovery-net.state"
        self.assertIn("stopped=1", state.read_text())
        # The portal address is lost out-of-band (restoring it is the re-run's
        # job), so the idempotent fast path does not short-circuit.  The client
        # service is already stopped, so the re-run's own stop call fails.
        (self.ip_state / "addr").unlink()
        result = self.up()
        self.assertEqual(result.returncode, 0, result.stderr)
        # The re-run must not overwrite the recorded ownership with stopped=0...
        self.assertIn("stopped=1", state.read_text())
        # ...so a later net-down still restores the client service.
        down = self.run_net(NET_DOWN, "--interface", "wlan0",
                            "--state-dir", str(self.state_dir))
        self.assertEqual(down.returncode, 0, down.stderr)
        self.assertIn("start", self.wpa_log_text())

    def test_net_down_retains_ownership_until_wifi_restart_succeeds(self) -> None:
        # net-up released the client plane and recorded the ownership it owes.
        self.assertEqual(self.up().returncode, 0)
        state = self.state_dir / "recovery-net.state"
        self.assertIn("stopped=1", state.read_text())
        # First teardown: the address is released, but restarting the client
        # wifi service fails (e.g. association/DHCP timeout).
        (self.root / "start_fail").write_text("1\n")
        first = self.run_net(NET_DOWN, "--interface", "wlan0",
                             "--state-dir", str(self.state_dir))
        self.assertNotEqual(first.returncode, 0)
        self.assertIn("cannot restore client service", first.stderr)
        # The obligation is retained, so a retry can still restore the client
        # plane instead of taking the nothing-owned no-op path forever.
        self.assertTrue(state.is_file())
        self.assertIn("stopped=1", state.read_text())
        # Retry: the wifi service comes back, the retry completes and clears the
        # record only once restoration has actually succeeded.
        second = self.run_net(NET_DOWN, "--interface", "wlan0",
                              "--state-dir", str(self.state_dir))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse(state.exists())
        self.assertIn("start", self.wpa_log_text())
        # The owned address was released once already; the retry must not try to
        # delete it a second time (it is idempotent, but only one del is owed).
        self.assertEqual(self.ip_log_text().count("addr del"), 1)

    def test_net_up_rolls_back_on_address_failure(self) -> None:
        (self.ip_state / "fail_add").touch()
        result = self.up()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot assign", result.stderr)
        self.assertFalse((self.state_dir / "recovery-net.state").exists())
        # the client service this call stopped must have been restored.
        self.assertIn("start", self.wpa_log_text())

    def test_net_up_refuses_unsafe_interface(self) -> None:
        result = self.run_net(NET_UP, "--interface", "wlan0;/bin/sh",
                              "--state-dir", str(self.state_dir))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe interface", result.stderr)

    def test_net_up_refuses_non_tmpfs_state_dir(self) -> None:
        result = self.run_net(NET_UP, "--interface", "wlan0",
                              "--state-dir", str(self.root / "persistent"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-tmpfs", result.stderr)

    def test_net_down_is_noop_without_recorded_ownership(self) -> None:
        result = self.run_net(NET_DOWN, "--interface", "wlan0",
                              "--state-dir", str(self.state_dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing-owned", result.stdout)
        self.assertEqual(self.wpa_log_text(), "")

    def test_net_down_removes_only_owned_address_and_restores_client(self) -> None:
        self.assertEqual(self.up().returncode, 0)
        result = self.run_net(NET_DOWN, "--interface", "wlan0",
                              "--state-dir", str(self.state_dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("addr del 192.168.4.1/24 dev wlan0", self.ip_log_text())
        self.assertFalse((self.state_dir / "recovery-net.state").exists())
        self.assertIn("start", self.wpa_log_text())

    def test_net_down_refuses_non_tmpfs_state_dir(self) -> None:
        result = self.run_net(NET_DOWN, "--interface", "wlan0",
                              "--state-dir", str(self.root / "persistent"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-tmpfs", result.stderr)

    def test_net_down_ignores_ownership_for_a_different_interface(self) -> None:
        self.assertEqual(self.up().returncode, 0)
        result = self.run_net(NET_DOWN, "--interface", "wlan1",
                              "--state-dir", str(self.state_dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing-owned", result.stdout)
        self.assertTrue((self.state_dir / "recovery-net.state").exists())


class DependencyPinTests(unittest.TestCase):
    @staticmethod
    def component(license_: str = "BSD-3-Clause") -> dict:
        return {
            "version": "2.10",
            "license": license_,
            "source_url": "https://example.invalid/hostapd-2.10.tar.gz",
            "source_sha256": "a" * 64,
            "source_license": "COPYING",
            "artifact": "hostapd",
        }

    @staticmethod
    def libnl_record() -> dict:
        return {
            "version": "3.11.0",
            "license": "LGPL-2.1-only",
            "source_url": "https://example.invalid/libnl-3.11.0.tar.gz",
            "source_sha256": "b" * 64,
            "source_license": "COPYING",
        }

    def write_lock(self, root: Path, component: dict, *,
                   source_offer: dict | None = None) -> Path:
        lock = {"components": {"hostapd": component},
                "build_dependencies": {"libnl": self.libnl_record()}}
        if source_offer is not None:
            lock["source_offer"] = source_offer
        path = root / "lock.json"
        path.write_text(json.dumps(lock))
        return path

    def test_lock_declares_valid_pins(self) -> None:
        lock = json.loads(LOCK.read_text())
        self.assertTrue(lock["components"])
        for name, component in lock["components"].items():
            with self.subTest(component=name):
                self.assertEqual(len(component["source_sha256"]), 64)
                int(component["source_sha256"], 16)
                self.assertTrue(component["source_url"].startswith("https://"))
                self.assertTrue(component["license"])
                # The declared licence text must be a real file name the
                # builder will require inside the upstream source tree.
                self.assertTrue(component["source_license"])
                self.assertTrue(component["artifact"])
        self.assertIn("libnl", lock["build_dependencies"])
        self.assertEqual(len(lock["build_dependencies"]["libnl"]["source_sha256"]), 64)

    def test_gpl_components_all_declare_a_source_offer(self) -> None:
        lock = json.loads(LOCK.read_text())
        offered = set(lock.get("source_offer", {}).get("components", []))
        for name, component in lock["components"].items():
            if "GPL" in component["license"]:
                self.assertIn(name, offered, f"{name} is GPL and needs a source offer")
                self.assertTrue(component["source_license"] or
                                component.get("source_license_secondary"))

    def run_builder(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(BUILDER), *args], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=120)

    def test_builder_accepts_matching_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "hostapd-2.10.tar.gz"
            archive.write_bytes(b"pinned-artifact-bytes")
            component = self.component()
            component["source_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            lock = self.write_lock(root, component)
            result = self.run_builder("--verify", "--lock", str(lock),
                                      "--archive", f"hostapd={archive}")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("verified hostapd 2.10", result.stdout)

    def test_builder_refuses_wrong_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "hostapd-2.10.tar.gz"
            archive.write_bytes(b"tampered")
            component = self.component()
            component["source_sha256"] = "0" * 64
            lock = self.write_lock(root, component)
            result = self.run_builder("--verify", "--lock", str(lock),
                                      "--archive", f"hostapd={archive}")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("mismatch", result.stderr)

    def test_builder_fails_closed_on_missing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = self.write_lock(root, self.component())
            result = self.run_builder("--verify", "--lock", str(lock))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no archive", result.stderr)

    def test_builder_rejects_malformed_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            component = self.component()
            component["source_sha256"] = "not-a-sha"
            lock = self.write_lock(root, component)
            result = self.run_builder("--verify", "--lock", str(lock))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("malformed", result.stderr)

    def test_builder_requires_a_source_offer_for_gpl_components(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "hostapd-2.10.tar.gz"
            archive.write_bytes(b"pinned-artifact-bytes")
            component = self.component("GPL-2.0-or-3.0")
            component["source_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            lock = self.write_lock(root, component)
            result = self.run_builder("--verify", "--lock", str(lock),
                                      "--archive", f"hostapd={archive}")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("corresponding-source offer", result.stderr)

    def test_builder_accepts_gpl_component_with_source_offer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "hostapd-2.10.tar.gz"
            archive.write_bytes(b"pinned-artifact-bytes")
            component = self.component("GPL-2.0-or-3.0")
            component["source_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            lock = self.write_lock(root, component,
                                   source_offer={"components": ["hostapd"]})
            result = self.run_builder("--verify", "--lock", str(lock),
                                      "--archive", f"hostapd={archive}")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("source-offer hostapd", result.stdout)


def load_tool(name: str):
    import importlib.util
    import sys

    path = TOOLS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"mt8163_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def arm32_static_elf(*, flags: int = 0x05000400) -> bytes:
    """A minimal 52-byte ARM32 EABI5 ELF header (no phdrs/shdrs)."""
    ident = b"\x7fELF" + bytes([1, 1, 1, 0]) + b"\x00" * 8
    rest = (
        (2).to_bytes(2, "little") + (40).to_bytes(2, "little") +
        (1).to_bytes(4, "little") + (0).to_bytes(4, "little") +
        (0).to_bytes(4, "little") + (0).to_bytes(4, "little") +
        flags.to_bytes(4, "little") +
        (52).to_bytes(2, "little") + (0).to_bytes(2, "little") * 5
    )
    data = ident + rest
    assert len(data) == 52, len(data)
    return data


# The pinned image targets plus the on-image licence copies recorded in
# recovery-ap/SOURCE.lock.  Kept here as an independent copy so drift in the
# production map fails these tests instead of silently agreeing with itself.
RECOVERY_AP_PATHS = {
    "hostapd": "usr/local/sbin/hostapd",
    "dnsmasq": "usr/local/sbin/dnsmasq",
    "iw": "usr/local/sbin/iw",
    "libreecho-recovery-button": "usr/local/sbin/libreecho-recovery-button",
}
RECOVERY_AP_LICENSES = {
    "hostapd": "BSD-3-Clause",
    "dnsmasq": "GPL-2.0-or-3.0",
    "iw": "ISC",
    "libreecho-recovery-button": "GPL-2.0-only",
}
RECOVERY_AP_IMAGE_LICENSES = {
    "hostapd": ["hostapd-2.10-COPYING.txt"],
    "dnsmasq": ["dnsmasq-2.90-COPYING.txt", "dnsmasq-2.90-COPYING-v3.txt"],
    "iw": ["iw-5.19-COPYING.txt"],
}
RECOVERY_AP_LICENSE_COPY_SHA256 = {
    "hostapd-2.10-COPYING.txt":
        "a87ac4e333d0f120408a9d814e40c3672cd27f365af89b4f2f6631f7a9338953",
    "dnsmasq-2.90-COPYING.txt":
        "8177f97513213526df2cf6184d8ff986c675afb514d4e68a404010521b880643",
    "dnsmasq-2.90-COPYING-v3.txt":
        "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903",
    "iw-5.19-COPYING.txt":
        "5932fb70481e128940168a5fdf133e6454293c0865c7e757874b235cb6daa2af",
}
RECOVERY_AP_LICENSE_ROOT = "usr/local/share/licenses/libreecho-core"

WORKFLOW = TOOLS_DIR.parents[1] / ".github" / "workflows" / "button-backport.yml"


def recovery_ap_metadata(hashes: dict) -> dict:
    """The exact document build_recovery_ap.sh --emit-metadata produces."""
    components = {}
    for name, relative in RECOVERY_AP_PATHS.items():
        entry = {
            "license": RECOVERY_AP_LICENSES[name],
            "source_url": "https://example.invalid/recovery-ap",
            "artifact": name,
            "image_path": relative,
        }
        if name == "libreecho-recovery-button":
            entry["source_path"] = (
                "tools/mt8163-arm32/recovery-ap/libreecho-recovery-button.c")
        else:
            entry["source_sha256"] = "a" * 64
            entry["source_license"] = "COPYING"
            entry["image_license"] = list(RECOVERY_AP_IMAGE_LICENSES[name])
        components[name] = entry
    return {
        "schema": "libreecho-recovery-ap-binaries/v1",
        "components": components,
        "binaries": {name: {"sha256": digest} for name, digest in hashes.items()},
        "source_offer": {
            "components": ["dnsmasq"],
            "statement": "corresponding-source offer for the GPL components",
        },
    }


class RecoveryApVerifierTests(unittest.TestCase):
    """Negative metadata-verification tests against verify_recovery_image."""

    def setUp(self) -> None:
        self.verifier = load_tool("verify_recovery_image")
        self.elf = arm32_static_elf()

    def make_fixtures(self) -> tuple[dict, dict]:
        elf = self.elf
        digests = {name: hashlib.sha256(elf).hexdigest()
                   for name in self.verifier.RECOVERY_AP_COMPONENTS}
        components = {}
        entries = {}
        for name, relative in self.verifier.RECOVERY_AP_COMPONENTS.items():
            components[name] = {
                "path": f"/{relative}", "mode": "0755",
                "sha256": digests[name], "size": len(elf),
            }
            entries[relative] = self.verifier.Entry(relative, 0o100755, 0, 0, 0, elf)
        metadata = recovery_ap_metadata(digests)
        metadata_bytes = json.dumps(metadata).encode()
        entries[self.verifier.RECOVERY_AP_METADATA] = self.verifier.Entry(
            self.verifier.RECOVERY_AP_METADATA, 0o100644, 0, 0, 0, metadata_bytes)
        for key, relative in self.verifier.RECOVERY_AP_OVERLAY.items():
            entries[relative] = self.verifier.Entry(
                relative, 0o100755, 0, 0, 0, b"#!/bin/busybox sh\n")
        # The core licence bundle the recovery-AP metadata points at.
        for licence_name in RECOVERY_AP_LICENSE_COPY_SHA256:
            relative = f"{self.verifier.RECOVERY_AP_LICENSE_ROOT}/{licence_name}"
            entries[relative] = self.verifier.Entry(relative, 0o100644, 0, 0, 0, b"licence\n")
        manifest = {"recovery_ap": {
            "enabled": True,
            "marker": self.verifier.RECOVERY_AP_MARKER,
            "button_detector": "/" + self.verifier.RECOVERY_AP_COMPONENTS[
                "libreecho-recovery-button"],
            "components": components,
            "metadata": {
                "path": f"/{self.verifier.RECOVERY_AP_METADATA}",
                "sha256": hashlib.sha256(metadata_bytes).hexdigest(),
                "size": len(metadata_bytes),
                "mode": "0644",
            },
        }}
        for key, relative in self.verifier.RECOVERY_AP_OVERLAY.items():
            manifest["recovery_ap"][key] = f"/{relative}"
        self.entries = entries
        self.manifest = manifest
        self.metadata = metadata
        return entries, manifest

    def restage(self, metadata: dict) -> None:
        """Re-encode mutated metadata and re-point the staged entry/manifest."""
        blob = json.dumps(metadata).encode()
        self.entries[self.verifier.RECOVERY_AP_METADATA] = self.verifier.Entry(
            self.verifier.RECOVERY_AP_METADATA, 0o100644, 0, 0, 0, blob)
        self.manifest["recovery_ap"]["metadata"]["sha256"] = hashlib.sha256(blob).hexdigest()
        self.manifest["recovery_ap"]["metadata"]["size"] = len(blob)

    def mutated(self, mutate) -> dict:
        metadata = json.loads(json.dumps(self.metadata))
        mutate(metadata)
        return metadata

    def test_valid_record_passes(self) -> None:
        entries, manifest = self.make_fixtures()
        self.verifier.validate_recovery_ap(entries, manifest)

    def test_disabled_bundle_rejects_present_component(self) -> None:
        entries, manifest = self.make_fixtures()
        manifest["recovery_ap"] = {"enabled": False}
        with self.assertRaises(SystemExit):
            self.verifier.validate_recovery_ap(entries, manifest)

    def test_enabled_bundle_rejects_missing_metadata(self) -> None:
        entries, manifest = self.make_fixtures()
        del entries[self.verifier.RECOVERY_AP_METADATA]
        with self.assertRaises(SystemExit):
            self.verifier.validate_recovery_ap(entries, manifest)

    def test_enabled_bundle_rejects_component_hash_mismatch(self) -> None:
        entries, manifest = self.make_fixtures()
        manifest["recovery_ap"]["components"]["hostapd"]["sha256"] = "0" * 64
        with self.assertRaises(SystemExit):
            self.verifier.validate_recovery_ap(entries, manifest)

    def test_enabled_bundle_rejects_metadata_inventory_mismatch(self) -> None:
        entries, manifest = self.make_fixtures()
        del manifest["recovery_ap"]["components"]["iw"]
        with self.assertRaises(SystemExit):
            self.verifier.validate_recovery_ap(entries, manifest)

    def test_enabled_bundle_rejects_non_static_elf(self) -> None:
        entries, manifest = self.make_fixtures()
        bad = arm32_static_elf(flags=0)
        relative = self.verifier.RECOVERY_AP_COMPONENTS["iw"]
        entries[relative] = self.verifier.Entry(relative, 0o100755, 0, 0, 0, bad)
        manifest["recovery_ap"]["components"]["iw"]["sha256"] = hashlib.sha256(bad).hexdigest()
        manifest["recovery_ap"]["components"]["iw"]["size"] = len(bad)
        with self.assertRaises(SystemExit):
            self.verifier.validate_recovery_ap(entries, manifest)

    def assert_rejected(self, metadata: dict) -> None:
        self.restage(metadata)
        with self.assertRaises(SystemExit):
            self.verifier.validate_recovery_ap(self.entries, self.manifest)

    def test_enabled_bundle_rejects_absent_provenance(self) -> None:
        self.make_fixtures()
        self.assert_rejected(self.mutated(lambda m: m.pop("components")))

    def test_enabled_bundle_rejects_unlicensed_provenance(self) -> None:
        self.make_fixtures()
        self.assert_rejected(self.mutated(
            lambda m: m["components"]["hostapd"].pop("license")))

    def test_enabled_bundle_rejects_non_https_source(self) -> None:
        self.make_fixtures()
        self.assert_rejected(self.mutated(
            lambda m: m["components"]["iw"].__setitem__("source_url", "http://insecure")))

    def test_enabled_bundle_rejects_wrong_schema(self) -> None:
        self.make_fixtures()
        self.assert_rejected(self.mutated(lambda m: m.__setitem__("schema", "v2")))

    def test_enabled_bundle_rejects_absent_source_offer(self) -> None:
        self.make_fixtures()
        self.assert_rejected(self.mutated(lambda m: m.pop("source_offer")))

    def test_enabled_bundle_rejects_gpl_component_without_source_offer(self) -> None:
        self.make_fixtures()
        self.assert_rejected(self.mutated(
            lambda m: m["source_offer"].__setitem__("components", [])))

    def test_enabled_bundle_rejects_absent_on_image_licence_copies(self) -> None:
        self.make_fixtures()
        self.assert_rejected(self.mutated(
            lambda m: m["components"]["dnsmasq"].pop("image_license")))

    def test_enabled_bundle_rejects_missing_licence_file_on_image(self) -> None:
        self.make_fixtures()
        del self.entries[f"{self.verifier.RECOVERY_AP_LICENSE_ROOT}/iw-5.19-COPYING.txt"]
        with self.assertRaises(SystemExit):
            self.verifier.validate_recovery_ap(self.entries, self.manifest)

    def test_enabled_bundle_rejects_provenance_image_path_mismatch(self) -> None:
        self.make_fixtures()
        self.assert_rejected(self.mutated(
            lambda m: m["components"]["dnsmasq"].__setitem__("image_path", "usr/bin/dnsmasq")))


class BundleStagingNegativeTests(unittest.TestCase):
    """add_recovery_ap_bundle must refuse a half-shipped inventory."""

    def setUp(self) -> None:
        self.builder = load_tool("build_recovery_image")

    def test_metadata_inventory_mismatch_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "stage"
            stage.mkdir()
            metadata = Path(tmp) / "meta.json"
            metadata.write_text(json.dumps({"binaries": {"hostapd": {"sha256": "0" * 64}}}))
            binaries = {name: Path(tmp) / name
                        for name in self.builder.RECOVERY_AP_BINARIES}
            with self.assertRaises(SystemExit) as caught:
                self.builder.add_recovery_ap_bundle(stage, binaries, metadata, {})
            self.assertIn("does not match the pinned set", str(caught.exception))

    def test_invalid_metadata_json_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "stage"
            stage.mkdir()
            metadata = Path(tmp) / "meta.json"
            metadata.write_text("{not json")
            with self.assertRaises(SystemExit):
                self.builder.add_recovery_ap_bundle(stage, {}, metadata, {})


class ImageIntegrationTests(unittest.TestCase):
    """The overlay/pin integration the recovery-AP helpers depend on."""

    def setUp(self) -> None:
        self.builder = load_tool("build_recovery_image")
        self.verifier = load_tool("verify_recovery_image")
        self.init = (INITRAMFS / "libreecho-init").read_text()

    def test_init_hash_pins_match_the_source(self) -> None:
        digest = hashlib.sha256((INITRAMFS / "libreecho-init").read_bytes()).hexdigest()
        self.assertEqual(self.builder.RECOVERY_INIT_SHA256, digest)
        self.assertEqual(self.verifier.INIT_SHA256, digest)

    def test_button_detector_is_a_pinned_component_not_an_overlay_file(self) -> None:
        # The compiled detector ships as a hash-pinned component; the obsolete
        # shell detector must not exist to be mistaken for the overlay source.
        self.assertIn("libreecho-recovery-button", self.builder.RECOVERY_AP_BINARIES)
        self.assertEqual(
            self.verifier.RECOVERY_AP_COMPONENTS.get("libreecho-recovery-button"),
            self.builder.RECOVERY_AP_BINARIES["libreecho-recovery-button"])
        self.assertNotIn("libreecho-recovery-button", self.verifier.OVERLAY_TARGETS)
        self.assertNotIn("libreecho-recovery-button", self.verifier.OVERLAY_FILES)
        self.assertFalse((INITRAMFS / "libreecho-recovery-button").exists())

    def test_overlay_stages_helpers_at_expected_targets(self) -> None:
        expected = {
            "libreecho-recovery-ap-probe": "usr/local/sbin/libreecho-recovery-ap-probe",
            "libreecho-recovery-ap-ready": "usr/local/sbin/libreecho-recovery-ap-ready",
            "libreecho-recovery-net-up": "usr/local/sbin/libreecho-recovery-net-up",
            "libreecho-recovery-net-down": "usr/local/sbin/libreecho-recovery-net-down",
        }
        for name, target in expected.items():
            with self.subTest(name=name):
                self.assertEqual(self.verifier.OVERLAY_TARGETS.get(name), target)
                self.assertIn(name, self.verifier.OVERLAY_FILES)
                self.assertTrue((INITRAMFS / name).is_file())

    def test_init_detects_before_network_startup(self) -> None:
        self.assertIn("physical_recovery_probe", self.init)
        probe_call = self.init.index("physical_recovery_probe\n")
        network_start = self.init.index(
            "# Connectivity comes up on the boot path, not at the end of init"
        )
        self.assertLess(probe_call, network_start)
        self.assertIn("/usr/local/sbin/libreecho-recovery-button", self.init)
        self.assertIn("/run/libreecho/recovery-mode", self.init)
        # Bounded so a boot without a held button cannot stall on the helper.
        self.assertIn('timeout -k 1 "$PHYSICAL_RECOVERY_TIMEOUT"', self.init)

    def test_recovery_init_timeout_covers_the_compiled_detector_window(self) -> None:
        # The init timeout must outlast the compiled detector's own absolute cap
        # (DEFAULT_MAX_MS), otherwise a legitimate 5 s hold that starts late is
        # killed by init before the detector can arm.
        match = re.search(
            r"PHYSICAL_RECOVERY_TIMEOUT=\$\{PHYSICAL_RECOVERY_TIMEOUT:-([0-9]+)\}", self.init)
        self.assertIsNotNone(match, "init does not bound the detector with a timeout")
        timeout_ms = int(match.group(1)) * 1000
        cap = re.search(r"DEFAULT_MAX_MS\s+([0-9]+)", DETECTOR_SRC.read_text())
        self.assertIsNotNone(cap, "detector has no absolute cap to compare against")
        detector_cap_ms = int(cap.group(1))
        self.assertGreater(timeout_ms, detector_cap_ms)
        # ...and the detector must actually be invoked under that bound.
        self.assertIn("timeout -k 1 \"$PHYSICAL_RECOVERY_TIMEOUT\"", self.init)

    def test_recovery_ap_targets_match_the_pinned_set(self) -> None:
        # The production maps must agree with the independent test copy.
        self.assertEqual(dict(self.builder.RECOVERY_AP_BINARIES), RECOVERY_AP_PATHS)
        self.assertEqual(dict(self.verifier.RECOVERY_AP_COMPONENTS), RECOVERY_AP_PATHS)


class BundleStagingValidationTests(unittest.TestCase):
    """The provenance contract add_recovery_ap_bundle enforces (no readelf)."""

    def setUp(self) -> None:
        self.builder = load_tool("build_recovery_image")

    def metadata(self) -> dict:
        return recovery_ap_metadata({name: "0" * 64 for name in RECOVERY_AP_PATHS})

    def test_complete_provenance_is_accepted(self) -> None:
        result = self.builder.validate_recovery_ap_provenance(self.metadata())
        self.assertEqual(set(result), set(RECOVERY_AP_PATHS))

    def assert_rejected(self, metadata: dict) -> None:
        with self.assertRaises(SystemExit):
            self.builder.validate_recovery_ap_provenance(metadata)

    def test_absent_provenance_is_refused(self) -> None:
        metadata = self.metadata()
        del metadata["components"]
        self.assert_rejected(metadata)

    def test_missing_licence_is_refused(self) -> None:
        metadata = self.metadata()
        del metadata["components"]["hostapd"]["license"]
        self.assert_rejected(metadata)

    def test_non_https_source_is_refused(self) -> None:
        metadata = self.metadata()
        metadata["components"]["iw"]["source_url"] = "http://insecure"
        self.assert_rejected(metadata)

    def test_provenance_image_path_mismatch_is_refused(self) -> None:
        metadata = self.metadata()
        metadata["components"]["dnsmasq"]["image_path"] = "usr/bin/dnsmasq"
        self.assert_rejected(metadata)

    def test_gpl_component_without_source_offer_is_refused(self) -> None:
        metadata = self.metadata()
        metadata["source_offer"]["components"] = []
        self.assert_rejected(metadata)

    def test_absent_source_offer_is_refused(self) -> None:
        metadata = self.metadata()
        del metadata["source_offer"]
        self.assert_rejected(metadata)

    def test_absent_on_image_licence_copy_is_refused(self) -> None:
        metadata = self.metadata()
        del metadata["components"]["iw"]["image_license"]
        self.assert_rejected(metadata)

    def test_wrong_schema_is_refused(self) -> None:
        metadata = self.metadata()
        metadata["schema"] = "libreecho-recovery-ap-binaries/v2"
        self.assert_rejected(metadata)

    def test_unsafe_first_party_source_path_is_refused(self) -> None:
        metadata = self.metadata()
        metadata["components"]["libreecho-recovery-button"]["source_path"] = "../evil.c"
        self.assert_rejected(metadata)


class MetadataEmissionTests(unittest.TestCase):
    """build_recovery_ap.sh --emit-metadata binds artifacts to provenance."""

    def emit(self, output: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(BUILDER), "--emit-metadata", "--lock", str(LOCK),
             "--output", str(output)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

    def test_emission_matches_the_pinned_set_and_real_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            elf = arm32_static_elf()
            for name in RECOVERY_AP_PATHS:
                (output / name).write_bytes(elf + name.encode())
            result = self.emit(output)
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads((output / "recovery-ap-binaries.json").read_text())
            self.assertEqual(document["schema"], "libreecho-recovery-ap-binaries/v1")
            self.assertEqual(set(document["binaries"]), set(RECOVERY_AP_PATHS))
            self.assertEqual(set(document["components"]), set(RECOVERY_AP_PATHS))
            for name in RECOVERY_AP_PATHS:
                with self.subTest(component=name):
                    expected = hashlib.sha256((output / name).read_bytes()).hexdigest()
                    self.assertEqual(document["binaries"][name]["sha256"], expected)
                    provenance = document["components"][name]
                    self.assertTrue(provenance["license"])
                    self.assertTrue(provenance["source_url"].startswith("https://"))
                    self.assertEqual(provenance["image_path"], RECOVERY_AP_PATHS[name])
            self.assertEqual(document["source_offer"]["components"], ["dnsmasq"])
            for name, copies in RECOVERY_AP_IMAGE_LICENSES.items():
                self.assertEqual(document["components"][name]["image_license"], copies)

    def test_emission_refuses_a_missing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            elf = arm32_static_elf()
            for name in RECOVERY_AP_PATHS:
                (output / name).write_bytes(elf)
            (output / "iw").unlink()
            result = self.emit(output)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((output / "recovery-ap-binaries.json").exists())

    def test_emitted_metadata_satisfies_the_image_builder_contract(self) -> None:
        builder = load_tool("build_recovery_image")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            elf = arm32_static_elf()
            for name in RECOVERY_AP_PATHS:
                (output / name).write_bytes(elf)
            self.assertEqual(self.emit(output).returncode, 0)
            document = json.loads((output / "recovery-ap-binaries.json").read_text())
            builder.validate_recovery_ap_provenance(document)


class LicenseCatalogTests(unittest.TestCase):
    """The on-image catalogue and notices cover the recovery-AP binaries."""

    def setUp(self) -> None:
        self.core = INITRAMFS / "usr/local/share/licenses/libreecho-core"
        self.components = json.loads((self.core / "COMPONENTS.json").read_text())
        self.notices = (self.core / "THIRD_PARTY_NOTICES.md").read_text()

    def test_components_json_lists_the_recovery_ap_binaries(self) -> None:
        by_id = {entry["id"]: entry for entry in self.components["components"]}
        for name, licence, source_sha in (
            ("hostapd", "BSD-3-Clause",
             "206e7c799b678572c2e3d12030238784bc4a9f82323b0156b4c9466f1498915d"),
            ("dnsmasq", "GPL-2.0-or-3.0",
             "8e50309bd837bfec9649a812e066c09b6988b73d749b7d293c06c57d46a109e4"),
            ("iw", "ISC",
             "2a44676d28a87bbc232903d5d573e7618e4fae0cea3a1aff067a26fa66652b75"),
        ):
            with self.subTest(component=name):
                self.assertIn(name, by_id)
                self.assertEqual(by_id[name]["license"], licence)
                self.assertEqual(by_id[name]["source_archive_sha256"], source_sha)
                self.assertTrue(by_id[name]["source"].startswith("https://"))

    def test_notices_document_the_recovery_ap_and_its_source_offer(self) -> None:
        for token in ("Recovery access point", "hostapd 2.10", "dnsmasq 2.90", "iw 5.19"):
            with self.subTest(token=token):
                self.assertIn(token, self.notices)
        self.assertIn("https://thekelleys.org.uk/dnsmasq/dnsmasq-2.90.tar.xz", self.notices)

    def test_every_named_on_image_licence_copy_ships_verbatim(self) -> None:
        for name, digest in RECOVERY_AP_LICENSE_COPY_SHA256.items():
            with self.subTest(file=name):
                data = (self.core / name).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
                self.assertTrue(data.strip())

    def test_source_lock_on_image_licences_match_the_shipped_copies(self) -> None:
        lock = json.loads(LOCK.read_text())
        on_image = lock["on_image"]
        self.assertEqual(on_image["license_root"], RECOVERY_AP_LICENSE_ROOT)
        self.assertEqual(set(on_image["licenses"]), set(RECOVERY_AP_IMAGE_LICENSES))
        for name, copies in on_image["licenses"].items():
            with self.subTest(component=name):
                self.assertEqual(copies, RECOVERY_AP_IMAGE_LICENSES[name])
                for copy in copies:
                    self.assertTrue((self.core / copy).is_file(), copy)


class WorkflowBuildGateTests(unittest.TestCase):
    """The CI workflow really builds and validates the pinned AP binaries."""

    def setUp(self) -> None:
        self.text = WORKFLOW.read_text()
        self.assertTrue(WORKFLOW.is_file(), WORKFLOW)

    def test_workflow_runs_the_real_recovery_build(self) -> None:
        self.assertIn("recovery-ap-build:", self.text)
        self.assertIn("build_recovery_ap.sh --build", self.text)
        self.assertIn("recovery-ap-binaries.json", self.text)

    def test_workflow_fetches_only_the_pinned_sources(self) -> None:
        # The fetch is pin-driven (SOURCE.lock), not a hand-written URL list.
        self.assertIn("recovery-ap/SOURCE.lock", self.text)
        self.assertIn('lock["components"]', self.text)
        self.assertIn("recovery_ap_source_%s_sha256", self.text)

    def test_workflow_uses_a_real_public_toolchain_not_secrets(self) -> None:
        self.assertIn("gcc-arm-linux-gnueabihf", self.text)
        self.assertIn("--cc arm-linux-gnueabihf-gcc", self.text)
        self.assertNotIn("secrets.", self.text)

    def test_workflow_validates_emitted_metadata_through_the_image_builder(self) -> None:
        self.assertIn("add_recovery_ap_bundle", self.text)
        self.assertIn("RECOVERY_AP_BINARIES", self.text)


if __name__ == "__main__":
    unittest.main()

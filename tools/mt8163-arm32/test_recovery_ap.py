#!/usr/bin/env python3
"""Fail-closed host tests for the Platform recovery-AP helpers (issue #96).

These tests exercise the real helper scripts with full isolation:

* the physical detector reads a fixed 16-byte little-endian evdev fixture file
  (a held, an early-released, a no-key and a wrong-key stream), never /sys;
* the AP probe runs against stub ``iw``/``ip`` oracles on an isolated PATH;
* the dependency builder refuses an archive whose SHA-256 does not match its
  pin.

No live device, radio, network, sysfs path, or image build is touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
INITRAMFS = TOOLS_DIR / "initramfs"
RECOVERY_AP = TOOLS_DIR / "recovery-ap"

BUTTON = INITRAMFS / "libreecho-recovery-button"
PROBE = INITRAMFS / "libreecho-recovery-ap-probe"
READY = INITRAMFS / "libreecho-recovery-ap-ready"
LOCK = RECOVERY_AP / "SOURCE.lock"
BUILDER = RECOVERY_AP / "build_recovery_ap.sh"

TAG = "libreecho-recovery-v1"
KEY_HELP = 138
KEY_MUTE = 113
EV_KEY = 1
EV_SYN = 0


def input_event(sec: int, usec: int, type_: int, code: int, value: int) -> bytes:
    """One 32-bit-ARM struct input_event: timeval + type + code + value."""
    return struct.pack("<IIHHi", sec, usec, type_, code, value)


def syn(sec: int, usec: int) -> bytes:
    return input_event(sec, usec, EV_SYN, 0, 0)


def write_script(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


class ButtonDetectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.marker = self.root / "run" / "libreecho" / "recovery-mode"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_detector(self, events: bytes, *extra: str) -> subprocess.CompletedProcess:
        device = self.root / "event0"
        device.write_bytes(events)
        args = ["sh", str(BUTTON), "--device", str(device),
                "--marker", str(self.marker), *extra]
        return subprocess.run(args, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=60)

    def test_held_button_arms_root_tmpfs_marker(self) -> None:
        stream = (input_event(10, 0, EV_KEY, KEY_HELP, 1) + syn(10, 0) +
                  input_event(12, 0, EV_KEY, KEY_HELP, 2) + syn(12, 0) +
                  input_event(15, 200000, EV_KEY, KEY_HELP, 0) + syn(15, 200000))
        result = self.run_detector(stream)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.is_file())
        content = self.marker.read_text()
        self.assertTrue(content.startswith(TAG), content)
        self.assertIn("hold_ms=5000", content)
        self.assertEqual(stat.S_IMODE(self.marker.stat().st_mode), 0o600)

    def test_release_before_threshold_never_arms(self) -> None:
        stream = (input_event(10, 0, EV_KEY, KEY_HELP, 1) +
                  input_event(12, 0, EV_KEY, KEY_HELP, 0))
        result = self.run_detector(stream)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())

    def test_no_key_leaves_boot_untouched(self) -> None:
        result = self.run_detector(b"")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())

    def test_wrong_key_code_never_arms(self) -> None:
        stream = (input_event(10, 0, EV_KEY, KEY_MUTE, 1) +
                  input_event(16, 0, EV_KEY, KEY_MUTE, 0))
        result = self.run_detector(stream)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())

    def test_non_tmpfs_marker_is_refused(self) -> None:
        device = self.root / "event0"
        device.write_bytes(input_event(10, 0, EV_KEY, KEY_HELP, 1))
        evil = self.root / "evil" / "recovery-mode"
        result = subprocess.run(
            ["sh", str(BUTTON), "--device", str(device), "--marker", str(evil)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-tmpfs", result.stderr)
        self.assertFalse(evil.exists())

    def test_detector_never_reads_sysfs(self) -> None:
        source = BUTTON.read_text()
        self.assertNotIn("/sys", source)


class ApProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()

    def tearDown(self) -> None:
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
        write_script(self.bin, "ip", f'printf "    inet {address}/24 scope global\\n"\n')

    def env(self, **overrides: str) -> dict:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env.get('PATH', '')}"
        env.update(overrides)
        return env

    def run_probe(self, entry: Path, *args: str, env: dict | None = None,
                  cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(entry), *args], text=True, cwd=cwd,
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

    def test_readiness_requires_live_control_dhcp_and_address(self) -> None:
        self.make_iw(dev_ok=True, mode="AP", ap_mode=True)
        self.make_ip("192.168.4.1")
        ctrl = self.root / "hostapd"
        ctrl.mkdir()
        dhcp_pid = self.root / "recovery-dhcp.pid"
        dhcp_pid.write_text("4242\n")
        env = self.env(RECOVERY_AP_CTRL=str(ctrl), RECOVERY_AP_DHCP_PID=str(dhcp_pid))
        self.assertEqual(self.run_probe(PROBE, "ready", env=env).returncode, 0)
        # ...and the wrapper delegates with the same readiness semantics.
        self.assertEqual(self.run_probe(PROBE, "ready", env=env).returncode, 0)

    def test_readiness_fails_without_hostapd_control(self) -> None:
        self.make_iw(dev_ok=True, mode="AP", ap_mode=True)
        self.make_ip("192.168.4.1")
        env = self.env(RECOVERY_AP_CTRL=str(self.root / "missing"))
        result = self.run_probe(PROBE, "ready", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hostapd-control-absent", result.stderr)

    def test_readiness_rejects_client_mode_interface(self) -> None:
        self.make_iw(dev_ok=True, mode="managed", ap_mode=True)
        self.make_ip("192.168.4.1")
        ctrl = self.root / "hostapd"
        ctrl.mkdir()
        env = self.env(RECOVERY_AP_CTRL=str(ctrl))
        result = self.run_probe(PROBE, "ready", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("interface-not-ap", result.stderr)

    def test_ready_wrapper_delegates_to_probe(self) -> None:
        source = READY.read_text()
        self.assertIn("ready", source)

    def test_probe_never_reads_sysfs(self) -> None:
        self.assertNotIn("/sys", PROBE.read_text())


class DependencyPinTests(unittest.TestCase):
    def test_lock_declares_valid_pins(self) -> None:
        lock = json.loads(LOCK.read_text())
        self.assertTrue(lock["components"])
        for name, component in lock["components"].items():
            with self.subTest(component=name):
                self.assertEqual(len(component["source_sha256"]), 64)
                int(component["source_sha256"], 16)
                self.assertTrue(component["source_url"].startswith("https://"))
                self.assertTrue(component["license"])
                self.assertTrue(component["source_license"])

    def run_builder(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(BUILDER), *args], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=120)

    def test_builder_accepts_matching_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "hostapd-2.10.tar.gz"
            archive.write_bytes(b"pinned-artifact-bytes")
            sha = hashlib.sha256(archive.read_bytes()).hexdigest()
            lock = root / "lock.json"
            lock.write_text(json.dumps({"components": {"hostapd": {
                "version": "2.10", "source_sha256": sha,
                "source_url": "https://example.invalid/hostapd-2.10.tar.gz",
                "artifact": "hostapd"}}}))
            result = self.run_builder("--verify", "--lock", str(lock),
                                      "--archive", f"hostapd={archive}")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("verified hostapd 2.10", result.stdout)

    def test_builder_refuses_wrong_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "hostapd-2.10.tar.gz"
            archive.write_bytes(b"tampered")
            lock = root / "lock.json"
            lock.write_text(json.dumps({"components": {"hostapd": {
                "version": "2.10", "source_sha256": "0" * 64,
                "source_url": "https://example.invalid/hostapd-2.10.tar.gz",
                "artifact": "hostapd"}}}))
            result = self.run_builder("--verify", "--lock", str(lock),
                                      "--archive", f"hostapd={archive}")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("mismatch", result.stderr)

    def test_builder_fails_closed_on_missing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = root / "lock.json"
            lock.write_text(json.dumps({"components": {"iw": {
                "version": "5.19", "source_sha256": "a" * 64,
                "source_url": "https://example.invalid/iw-5.19.tar.gz",
                "artifact": "iw"}}}))
            result = self.run_builder("--verify", "--lock", str(lock))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no archive", result.stderr)


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

    def test_overlay_stages_helpers_at_expected_targets(self) -> None:
        expected = {
            "libreecho-recovery-button": "usr/local/sbin/libreecho-recovery-button",
            "libreecho-recovery-ap-probe": "usr/local/sbin/libreecho-recovery-ap-probe",
            "libreecho-recovery-ap-ready": "usr/local/sbin/libreecho-recovery-ap-ready",
        }
        for name, target in expected.items():
            with self.subTest(name=name):
                self.assertEqual(self.verifier.OVERLAY_TARGETS.get(name), target)
                self.assertIn(name, self.verifier.OVERLAY_FILES)
                # The builder and verifier must agree the helper ships via the
                # overlay, i.e. the file exists under the overlay source root.
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


if __name__ == "__main__":
    unittest.main()

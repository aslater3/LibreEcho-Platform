#!/usr/bin/env python3
"""Host-side test for the boot-path adoption of a TWRP-staged install.

A TWRP install leaves a complete staged tree and both boot slots written, but
nothing it can run in recovery prepares the feature transaction. The first boot
of the image it wrote does that, and ``adopt_staged_local_install`` in
``libreecho-init`` is the decision.

That decision is on the boot path, so it is exercised here by extracting the
function from ``libreecho-init`` and running it under a real shell with the
paths it reads pointed at a scratch tree. The properties under test are the ones
that keep an unattended adopt from being dangerous:

* it fires only when the staged SIGNED manifest names the boot image that is
  actually running, which is what binds a staged tree to the install that made
  it;
* it prepares the transaction for the RUNNING slot -- the OTA verb targets
  ``inactive_slot``, so using it here would prepare a candidate the booted slot
  is not, and this boot would take the failed-candidate path;
* it does nothing at all when a transaction already exists.
"""

from __future__ import annotations

import hashlib
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
INIT = HERE / "initramfs/libreecho-init"


def extract_function(source: str, name: str) -> str:
    """Return the text of a top-level shell function, braces balanced."""
    marker = f"{name}()\n{{\n"
    start = source.index(marker)
    depth = 0
    for offset, char in enumerate(source[start:], start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:offset + 1]
    raise AssertionError(f"unterminated function: {name}")


class StagedLocalInstallAdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="le-adopt-test-"))
        self.update_root = self.work / "update"
        (self.update_root / "staging/features/tts").mkdir(parents=True)
        self.staging_manifest = self.update_root / "staging/manifest"
        self.package = self.update_root / "incoming/local-install.ota.tar"
        self.package.parent.mkdir(parents=True)
        self.package.write_bytes(b"package-bytes")
        self.cmdline = self.work / "cmdline"
        self.cmdline.write_text("console=ttyS0 androidboot.slot_suffix=_a\n")
        self.image = self.work / "boot_a.img"
        self.image.write_bytes(b"the running image")
        self.other_image = self.work / "boot_b.img"
        self.other_image.write_bytes(b"some other image")
        self.log = self.work / "init.log"
        self.record = self.work / "invocation"
        # A stub transaction tool that records each verb it is handed. preflight
        # is expected to fail or pass independently of prepare-boot, which is why
        # the second call is recorded by its own line.
        self.transaction = self.work / "libreecho-feature-transaction"
        self.transaction.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{self.record}"\n'
            'case "${1:-}" in\n'
            "  preflight) exit ${STUB_PREFLIGHT_RC:-0} ;;\n"
            "  *) exit ${STUB_RC:-0} ;;\n"
            "esac\n")
        self.transaction.chmod(self.transaction.stat().st_mode | stat.S_IEXEC)
        # Stub boot control tool: it is what running_slot_id asks when the
        # bootloader reports no slot suffix, which is what this hardware does.
        self.bootctl = self.work / "libreecho-bootctl"
        self.bootctl.write_text(
            "#!/bin/sh\n"
            f'printf "bootctl %s\\n" "$*" >> "{self.record}"\n'
            'printf "selected_slot=%s\\n" "${BOOTCTL_SLOT:-}"\n')
        self.bootctl.chmod(self.bootctl.stat().st_mode | stat.S_IEXEC)

    def tearDown(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def run_adopt(self, *, image_profile: str = "ota", boot_hash: str | None = None,
                  preflight_rc: int = 0, stub_rc: int = 0, cmdline: str | None = None,
                  bootctl_slot: str = "") -> subprocess.CompletedProcess[str]:
        # The staged manifest is signed, so its boot_sha256 is what binds the tree
        # to an image; point it at whichever image the test wants "running".
        self.staging_manifest.write_text(
            "format=libreecho-ota-v2\n"
            "feature_ids=tts\n"
            f"boot_sha256={boot_hash if boot_hash is not None else self.boot_a_hash()}\n")
        function = extract_function(INIT.read_text(), "running_slot_id")
        function += "\n" + extract_function(INIT.read_text(), "adopt_staged_local_install")
        harness = self.work / "harness.sh"
        harness.write_text(
            "#!/bin/sh\n"
            "set -u\n"
            "BB=\n"
            f'LOG="{self.log}"\n'
            'log() { printf "%s\\n" "$*" >> "$LOG"; }\n'
            'pmsg_marker() { printf "PMSG %s\\n" "$*" >> "$LOG"; }\n'
            f'IMAGE_PROFILE="{image_profile}"\n'
            f'export LIBREECHO_INSTALL_ROOT="{self.update_root}"\n'
            f'export LIBREECHO_CMDLINE_FILE="{self.cmdline}"\n'
            f'export LIBREECHO_SLOT_A_DEVICE="{self.image}"\n'
            f'export LIBREECHO_SLOT_B_DEVICE="{self.other_image}"\n'
            f'export LIBREECHO_TRANSACTION_TOOL="{self.transaction}"\n'
            f'export LIBREECHO_BOOTCTL_TOOL="{self.bootctl}"\n'
            f'export BOOTCTL_SLOT="{bootctl_slot}"\n'
            f'export STUB_PREFLIGHT_RC="{preflight_rc}"\n'
            f'export STUB_RC="{stub_rc}"\n'
            f"{function}\n"
            "adopt_staged_local_install\n"
            'printf "rc=%s\n" "$?"\n'
            'printf "slot=%s\n" "$(running_slot_id)"\n')
        if cmdline is not None:
            self.cmdline.write_text(cmdline)
        return subprocess.run(["sh", str(harness)], text=True, capture_output=True)

    def logged(self) -> str:
        return self.log.read_text() if self.log.is_file() else ""

    def invocations(self) -> list[str]:
        if not self.record.is_file():
            return []
        return self.record.read_text().splitlines()

    def boot_a_hash(self) -> str:
        return hashlib.sha256(self.image.read_bytes()).hexdigest()

    def test_a_staged_tree_for_the_running_image_is_adopted(self) -> None:
        result = self.run_adopt()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rc=0", result.stdout)
        self.assertEqual(self.invocations(), [
            f"preflight {self.update_root}/staging/manifest prewrite",
            "prepare-boot",
        ])
        self.assertIn("local-install-adopting:a", self.logged())
        self.assertIn("local-install-prepared", self.logged())
        self.assertIn("PMSG local-install-prepared", self.logged())

    def test_the_transaction_belongs_to_the_running_slot(self) -> None:
        # Not the inactive one: prepare-boot takes its slot from this file, and a
        # candidate the booted slot is not sends this boot down the
        # failed-candidate path.
        self.run_adopt()
        slot_file = self.update_root / "transaction-slot"
        self.assertTrue(slot_file.is_file())
        self.assertEqual(slot_file.read_text(), "a\n")
        self.assertFalse((self.update_root / "transaction-slot.tmp").exists())

    def test_slot_b_is_adopted_when_b_is_running(self) -> None:
        self.cmdline.write_text("androidboot.slot_suffix=_b\n")
        self.run_adopt(boot_hash=hashlib.sha256(self.other_image.read_bytes()).hexdigest())
        self.assertEqual((self.update_root / "transaction-slot").read_text(), "b\n")

    def test_a_tree_for_a_different_image_is_left_alone(self) -> None:
        result = self.run_adopt(boot_hash="cd" * 32)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.invocations(), [])
        self.assertFalse((self.update_root / "transaction-slot").exists())
        self.assertIn("local-install-staged-tree-mismatch:a", self.logged())
        self.assertNotIn("local-install-adopting", self.logged())

    def test_an_existing_transaction_is_left_alone(self) -> None:
        (self.update_root / "feature-commit").write_text("schema=2\nphase=prepared\n")
        result = self.run_adopt()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.invocations(), [])
        self.assertIn("local-install-transaction-present:feature-commit", self.logged())

    def test_a_diagnostic_image_never_adopts(self) -> None:
        result = self.run_adopt(image_profile="diagnostic")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.invocations(), [])
        self.assertNotIn("local-install", self.logged())

    def test_an_unreadable_manifest_is_refused(self) -> None:
        result = self.run_adopt(boot_hash="not-a-digest")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.invocations(), [])
        self.assertIn("local-install-manifest-unreadable", self.logged())

    def test_an_empty_cmdline_suffix_falls_back_to_boot_control(self) -> None:
        # Measured on the biscuit Dot: the cmdline carries the key with no value
        # (`androidboot.slot_suffix=_`), so the bootloader never says which slot
        # is running. Refusing here would leave every staged tree unadopted on
        # exactly the hardware this is for.
        result = self.run_adopt(cmdline="console=ttyS0 androidboot.slot_suffix=_\n",
                                bootctl_slot="a")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("slot=a", result.stdout)
        self.assertEqual((self.update_root / "transaction-slot").read_text(), "a\n")
        self.assertIn("local-install-prepared", self.logged())
        self.assertIn("bootctl status", self.invocations())

    def test_the_cmdline_wins_when_it_does_report_a_slot(self) -> None:
        # A bootloader that does report the slot is authoritative; the boot
        # control block is only consulted when it stays silent.
        result = self.run_adopt(cmdline="androidboot.slot_suffix=_a\n",
                                bootctl_slot="b")
        self.assertIn("slot=a", result.stdout)
        self.assertNotIn("bootctl status", self.invocations())

    def test_silence_from_both_sources_is_refused(self) -> None:
        result = self.run_adopt(cmdline="console=ttyS0\n", bootctl_slot="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("slot=", result.stdout)
        self.assertFalse((self.update_root / "transaction-slot").exists())
        self.assertIn("local-install-running-slot-unknown", self.logged())

    def test_a_missing_transaction_tool_is_reported(self) -> None:
        self.transaction.unlink()
        result = self.run_adopt()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.invocations(), [])
        self.assertIn("local-install-transaction-unavailable", self.logged())

    def test_a_failed_preflight_does_not_write_the_slot(self) -> None:
        result = self.run_adopt(preflight_rc=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.invocations(), [
            f"preflight {self.update_root}/staging/manifest prewrite"])
        self.assertIn("local-install-prepare-failed", self.logged())

    def test_a_failed_prepare_is_reported_and_does_not_block_boot(self) -> None:
        # The staged tree is this image's, so a prepare that fails is worth
        # shouting about -- but services still start, because an appliance that
        # never comes up cannot be diagnosed over the network.
        result = self.run_adopt(stub_rc=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("prepare-boot", self.invocations())
        self.assertIn("local-install-prepare-failed", self.logged())
        self.assertIn("PMSG local-install-preparation-failed", self.logged())


class RunningSlotFallbackTests(unittest.TestCase):
    """The candidate mount needs the same answer as the adoption.

    ``activate_feature_transaction`` compares the pending slot against the
    running slot before it mounts a staged candidate. With a silent cmdline that
    comparison used to fail, log `feature-transaction-fallback-canonical` and
    return early -- so a freshly installed unit would come up without its
    features on the boot that installs them.
    """

    def test_activate_feature_transaction_asks_running_slot_id(self) -> None:
        init = INIT.read_text()
        body = extract_function(init, "activate_feature_transaction")
        self.assertIn("running_slot=$(running_slot_id)", body)
        self.assertNotIn("androidboot.slot_suffix", body)

    def test_running_slot_id_is_defined_before_it_is_used(self) -> None:
        init = INIT.read_text()
        self.assertLess(init.index("running_slot_id()\n{"), init.index("activate_feature_transaction()\n{"))
        self.assertLess(init.index("running_slot_id()\n{"), init.index("adopt_staged_local_install()\n{"))

    def test_the_boot_control_fallback_is_overridable(self) -> None:
        init = INIT.read_text()
        body = extract_function(init, "running_slot_id")
        self.assertIn("LIBREECHO_BOOTCTL_TOOL", body)
        self.assertIn("LIBREECHO_CMDLINE_FILE", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)

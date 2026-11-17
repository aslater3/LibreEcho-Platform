"""An OTA install lock left by an earlier boot must never block updates.

The lock is persistent (/data), so an updater killed by a reboot leaves it
behind (issue #356: a restart inside `confirm` left `install.lock` and every
later check failed `update_busy`).  These tests execute the shipped shell
functions with real BusyBox against a fake root and a fake boot_id.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
UPDATE = HERE / "initramfs/libreecho-update"
FETCH = HERE / "initramfs/libreecho-update-fetch"
INIT = HERE / "initramfs/libreecho-init"
BB = "/bin/busybox"
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def function_text(source: str, name: str) -> str:
    start = source.index(f"\n{name}()\n{{\n") + 1
    return source[start:source.index("\n}\n", start) + 3]


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.update = self.root / "data/libreecho/update"
        self.update.mkdir(parents=True)
        self.lock = self.update / "install.lock"
        self.boot_file = self.root / "boot_id"
        self.set_boot("boot-aaaa")

    def set_boot(self, value: str) -> None:
        self.boot_file.write_text(value + "\n")

    def sh(self, script: str) -> subprocess.CompletedProcess:
        path = self.root / "run.sh"
        path.write_text(script.replace(BOOT_ID_PATH, str(self.boot_file)))
        return subprocess.run([BB, "sh", str(path)], capture_output=True,
                              text=True, timeout=30)

    def plant(self, owner: str | None, boot: str | None = None) -> None:
        self.lock.mkdir()
        if owner is not None:
            lines = f"owner={owner}\n"
            if boot is not None:
                lines += f"boot_id={boot}\n"
            (self.lock / "owner").write_text(lines + "pid=1234\n")


class UpdaterLockTests(Fixture):
    def updater(self, entry: str) -> subprocess.CompletedProcess:
        source = UPDATE.read_text()
        source = source[:source.rfind('case "${1:-}" in')]
        source = source.replace("DATA_ROOT=/data", f"DATA_ROOT={self.root}/data")
        return self.sh(source + "\nrequire_userdata() { :; }\n" + entry + "\n")

    def test_lock_from_another_boot_is_reclaimed(self) -> None:
        self.plant("update", "boot-old")
        result = self.updater('lock_take; echo TOOK; cat "$LOCK/owner"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TOOK", result.stdout)
        self.assertIn("owner=update\nboot_id=boot-aaaa\npid=", result.stdout)
        # The EXIT trap releases the lock, owner file included.
        self.assertFalse(self.lock.exists())

    def test_rollback_resume_tag_from_another_boot_is_reclaimed(self) -> None:
        self.plant("rollback-resume", "boot-old")
        result = self.updater("lock_take; echo TOOK")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TOOK", result.stdout)

    def test_lock_from_this_boot_stays_busy(self) -> None:
        self.plant("update", "boot-aaaa")
        result = self.updater("lock_take; echo TOOK")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("TOOK", result.stdout)
        self.assertIn("update_busy", result.stderr)
        self.assertTrue((self.lock / "owner").exists())

    def test_untagged_lock_is_not_reclaimed_by_the_updater(self) -> None:
        # Only the early-boot sweep can prove an untagged lock is old.
        self.plant(None)
        result = self.updater("lock_take; echo TOOK")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("update_busy", result.stderr)
        self.assertTrue(self.lock.is_dir())

    def test_owner_without_boot_id_stays_busy(self) -> None:
        self.plant("update", None)
        result = self.updater("lock_take; echo TOOK")
        self.assertIn("update_busy", result.stderr)
        self.assertTrue(self.lock.is_dir())

    def test_failed_lock_take_does_not_remove_the_holders_lock(self) -> None:
        self.plant("update", "boot-aaaa")
        self.updater("lock_take")
        self.assertTrue((self.lock / "owner").exists())

    def test_confirm_after_interrupted_confirm_in_an_earlier_boot(self) -> None:
        """Issue #356: confirm killed after bootctl, next boot confirms again."""
        (self.update / "pending").write_text("schema=3\nslot=b\n")
        stub = self.root / "bootctl"
        stub.write_text("#!/bin/sh\n[ -z \"$KILL_AFTER_CONFIRM\" ] || kill -9 $PPID\necho confirmed\n")
        stub.chmod(0o755)
        entry = (
            "target_device_for_slot() { :; }\n"
            f"BOOTCTL={stub}\n"
            "FEATURE_TRANSACTION=/bin/true\n"
            "publish_confirmed_check_status() { :; }\n"
            "state_write() { echo \"state=$1\" >> \"$UPDATE_ROOT/states\"; }\n"
            "confirm_pending\n"
        )
        # Boot A: the process dies inside confirm; the lock survives.
        self.updater("KILL_AFTER_CONFIRM=1\nexport KILL_AFTER_CONFIRM\n" + entry)
        self.assertTrue((self.lock / "owner").exists())
        self.assertFalse((self.update / "states").exists())
        # Same boot: still busy.
        busy = self.updater(entry)
        self.assertIn("update_busy", busy.stderr)
        # Boot B: the stale lock is reclaimed and confirm completes.
        self.set_boot("boot-bbbb")
        done = self.updater(entry)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("UPDATE_CONFIRMED slot=b", done.stdout)
        self.assertEqual((self.update / "states").read_text(), "state=installed\n")
        self.assertFalse(self.lock.exists())


class FetchLockTests(Fixture):
    def fetcher(self, entry: str) -> subprocess.CompletedProcess:
        source = FETCH.read_text()
        source = source[:source.rfind('case "${1:-}" in')]
        source = source.replace("ROOT=/data/libreecho/update", f"ROOT={self.update}")
        die = 'die() { echo "ERROR:$1" >&2; exit 1; }\n'
        return self.sh(source + "\n" + die + entry + "\n")

    def test_fetch_lock_is_boot_local(self) -> None:
        # The transport lock lives in /run (tmpfs), so it cannot outlive a boot.
        self.assertIn("LOCK=$RUN_ROOT/libreecho/fetch.lock", function_text(FETCH.read_text(), "fetch_lock"))

    def test_install_lock_from_another_boot_is_reclaimed(self) -> None:
        self.plant("update-fetch", "boot-old")
        result = self.fetcher('install_lock; echo TOOK; cat "$INSTALL_LOCK/owner"; install_unlock')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TOOK", result.stdout)
        self.assertIn("owner=update-fetch\nboot_id=boot-aaaa\npid=", result.stdout)
        self.assertFalse(self.lock.exists())

    def test_install_lock_from_this_boot_stays_busy(self) -> None:
        self.plant("update", "boot-aaaa")
        result = self.fetcher("install_lock; echo TOOK")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("update_busy", result.stderr)
        self.assertTrue((self.lock / "owner").exists())

    def test_untagged_install_lock_stays_busy(self) -> None:
        self.plant(None)
        result = self.fetcher("install_lock; echo TOOK")
        self.assertIn("update_busy", result.stderr)
        self.assertTrue(self.lock.is_dir())

    def test_exit_trap_releases_a_tagged_lock(self) -> None:
        result = self.fetcher("RUN_ROOT=" + str(self.root / "run") + "\ninstall_lock\nexit 0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.lock.exists())


class EarlyBootSweepTests(Fixture):
    def sweep(self) -> subprocess.CompletedProcess:
        init = INIT.read_text()
        body = function_text(init, "ota_stale_install_lock_sweep")
        body = body.replace("/data/libreecho/update", str(self.update))
        script = (f'BB={BB}\nlog() {{ echo "$1" >> "{self.root}/log"; }}\n'
                  + body + "\nota_stale_install_lock_sweep\n")
        return self.sh(script)

    def log(self) -> str:
        path = self.root / "log"
        return path.read_text() if path.exists() else ""

    def test_untagged_lock_is_swept_and_logged(self) -> None:
        self.plant(None)
        self.assertEqual(self.sweep().returncode, 0)
        self.assertFalse(self.lock.exists())
        self.assertEqual(self.log(), "ota-stale-install-lock-reclaimed:untagged\n")

    def test_lock_from_an_earlier_boot_is_swept_with_its_owner_logged(self) -> None:
        self.plant("update", "boot-old")
        self.sweep()
        self.assertFalse(self.lock.exists())
        self.assertEqual(self.log(), "ota-stale-install-lock-reclaimed:update\n")

    def test_lock_tagged_with_this_boot_is_kept(self) -> None:
        self.plant("update", "boot-aaaa")
        self.sweep()
        self.assertTrue(self.lock.is_dir())
        self.assertEqual(self.log(), "ota-stale-install-lock-kept:update\n")

    def test_no_lock_is_silent(self) -> None:
        self.sweep()
        self.assertEqual(self.log(), "")

    def test_symlinked_lock_is_not_followed(self) -> None:
        target = self.root / "elsewhere"
        target.mkdir()
        (target / "keep").write_text("x")
        self.lock.symlink_to(target)
        self.sweep()
        self.assertTrue((target / "keep").exists())

    def test_sweep_runs_before_any_updater_in_init(self) -> None:
        init = INIT.read_text()
        sweep = init.index("\n    ota_stale_install_lock_sweep\n")
        self.assertGreater(sweep, init.index("if userdata_mount; then"))
        # Everything that can take the lock is started after the sweep.
        for later in ("ota_health_confirm_worker()", "ota_rollback_resume_install_lock()",
                      "adopt_staged_local_install()", "start_ui_services()"):
            self.assertGreater(init.index(later), sweep, later)

    def test_tester_sequence_confirm_interrupted_then_next_boot(self) -> None:
        """Untagged lock (device in the field today) -> sweep -> checks work."""
        self.plant(None)
        self.set_boot("boot-bbbb")
        self.sweep()
        source = FETCH.read_text()
        source = source[:source.rfind('case "${1:-}" in')]
        source = source.replace("ROOT=/data/libreecho/update", f"ROOT={self.update}")
        result = self.sh(source + '\ndie() { echo "ERROR:$1" >&2; exit 1; }\n'
                         "install_lock; echo CHECK_OK; install_unlock\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CHECK_OK", result.stdout)


class PendingCheckTakesNoLockTests(Fixture):
    """A check/install while an update awaits confirmation never locks."""

    def run_check(self, action: str = "check") -> subprocess.CompletedProcess:
        source = FETCH.read_text()
        source = source[:source.rfind('case "${1:-}" in')]
        source = source.replace("ROOT=/data/libreecho/update", f"ROOT={self.update}")
        # Fail loudly if anything past the pending gate runs or locks.
        entry = (f"RUN_ROOT={self.root}/run\n"
                 "require_environment() { $BB mkdir -p \"$RUN_ROOT/libreecho\"; }\n"
                 "fetch_lock() { echo LOCKED_FETCH >&2; exit 97; }\n"
                 "install_lock() { echo LOCKED_INSTALL >&2; exit 98; }\n"
                 f"check_or_install {action}\n")
        return self.sh(source + "\n" + entry)

    def test_check_while_pending_reports_pending_without_locking(self) -> None:
        (self.update / "pending").write_text("schema=3\nslot=b\n")
        for action in ("check", "install", "auto-install"):
            with self.subTest(action=action):
                result = self.run_check(action)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("ERROR:update_already_pending", result.stderr)
                self.assertNotIn("LOCKED", result.stderr)
                self.assertFalse(self.lock.exists())
                status = (self.update / "check-status").read_text()
                self.assertIn("status=error", status)
                self.assertIn("error=update_already_pending", status)

    def test_check_without_pending_still_takes_the_locks(self) -> None:
        result = self.run_check()
        self.assertEqual(result.returncode, 97, result.stderr)


class ConfirmLockWaitTests(Fixture):
    """The health worker's confirm waits out a same-boot fetcher's lock."""

    def run_confirm(self, release_after: float | None, tag: str | None = "update-fetch",
                    boot: str = "boot-aaaa") -> subprocess.CompletedProcess:
        stub = self.root / "libreecho-update"
        stub.write_text(
            "#!/bin/sh\n"
            f'if [ -d "{self.lock}" ]; then echo ERROR:update_busy >&2; exit 1; fi\n'
            f'echo confirmed >> "{self.root}/confirms"\nexit 0\n')
        stub.chmod(0o755)
        init = INIT.read_text()
        body = (function_text(init, "ota_confirm_with_lock_wait") + function_text(init, "ota_confirm_error_token"))
        body = (body.replace("/usr/local/sbin/libreecho-update", str(stub))
                    .replace("/tmp/ota-confirm.log", str(self.root / "confirm.log"))
                    .replace("/data/libreecho/update", str(self.update))
                    .replace("confirm_wait_max=120", "confirm_wait_max=3")
                    .replace("confirm_wait_step=5", "confirm_wait_step=1"))
        self.plant(tag, boot if tag else None)
        release = ""
        if release_after is not None:
            release = f'( sleep {release_after}; rm -rf "{self.lock}" ) &\n'
        script = (f'BB={BB}\nlog() {{ echo "$1" >> "{self.root}/log"; }}\n' + body + release
                  + "ota_confirm_with_lock_wait\necho rc=$?\nwait\n")
        return self.sh(script)

    def log_lines(self) -> list[str]:
        path = self.root / "log"
        return path.read_text().splitlines() if path.exists() else []

    def test_confirm_waits_and_succeeds_when_the_lock_is_released(self) -> None:
        result = self.run_confirm(release_after=1.5)
        self.assertIn("rc=0", result.stdout, result.stderr)
        self.assertEqual((self.root / "confirms").read_text(), "confirmed\n")
        self.assertEqual(self.log_lines(), ["ota-confirm-lock-wait:update-fetch"])

    def test_confirm_fails_unchanged_when_the_lock_is_never_released(self) -> None:
        result = self.run_confirm(release_after=None)
        self.assertIn("rc=1", result.stdout, result.stderr)
        self.assertFalse((self.root / "confirms").exists())
        self.assertEqual(self.log_lines(), ["ota-confirm-lock-wait:update-fetch"])
        self.assertIn("ERROR:update_busy", (self.root / "confirm.log").read_text())

    def test_lock_from_an_earlier_boot_is_not_waited_for(self) -> None:
        result = self.run_confirm(release_after=None, boot="boot-old")
        self.assertIn("rc=1", result.stdout, result.stderr)
        self.assertEqual(self.log_lines(), [])

    def test_untagged_lock_is_waited_for(self) -> None:
        result = self.run_confirm(release_after=1.5, tag=None)
        self.assertIn("rc=0", result.stdout, result.stderr)
        self.assertEqual(self.log_lines(), ["ota-confirm-lock-wait:untagged"])

    def test_confirm_succeeds_when_the_lock_is_already_gone(self) -> None:
        (self.root / "libreecho-update").write_text("")
        result = self.run_confirm(release_after=0)
        self.assertIn("rc=0", result.stdout, result.stderr)

    def test_window_covers_the_fetch_resolve_timeouts(self) -> None:
        init = INIT.read_text()
        fetch = FETCH.read_text()
        self.assertIn("confirm_wait_max=120", init)
        self.assertIn("confirm_wait_step=5", init)
        # mirror probe (20 s) + release pointer (60 s) is the lock-held span.
        self.assertIn('bounded_curl "$probe" "$CURL_HEADERS" 64 20 ', fetch)
        self.assertIn('bounded_curl "$pointer" "$CURL_HEADERS" 257 60 ', fetch)


if __name__ == "__main__":
    unittest.main()

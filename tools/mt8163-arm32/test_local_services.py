"""Host tests for the persistent local services supervisor (#170).

Runs the real initramfs/libreecho-local under BusyBox sh (the device shell) in a
temporary root with every path overridden. Never touches a device.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "initramfs/libreecho-local"
BUSYBOX = shutil.which("busybox")


@unittest.skipUnless(BUSYBOX, "busybox unavailable")
class LocalServicesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.local = base / "data/libreecho/local"
        self.services = self.local / "services"
        self.services.mkdir(parents=True)
        self.status = base / "run/libreecho/local.status"
        self.run_root = base / "run/libreecho-local"
        self.logs = base / "log"
        # BusyBox applets first, as on the device; host nice/ionice where the
        # host BusyBox build lacks them.
        bindir = base / "bin"
        bindir.mkdir()
        applets = subprocess.run([BUSYBOX, "--list"], capture_output=True, text=True).stdout.split()
        for applet in applets:
            os.symlink(BUSYBOX, bindir / applet)
        for host_tool in ("nice", "ionice"):
            if host_tool not in applets and shutil.which(host_tool):
                os.symlink(shutil.which(host_tool), bindir / host_tool)
        self.env = dict(os.environ, PATH=f"{bindir}", LOCAL_ROOT=str(self.local),
                        LOCAL_STATUS=str(self.status), LOCAL_RUN_ROOT=str(self.run_root),
                        LOCAL_LOG_ROOT=str(self.logs), LOCAL_POLL_SECONDS="0.2",
                        LOCAL_STOP_GRACE_SECONDS="2", LOCAL_BACKOFF_MAX_SECONDS="1",
                        LOCAL_MAX_FAILURES="3", LOCAL_FAILURE_WINDOW_SECONDS="600")
        self.base = base
        self.background = None

    def tearDown(self):
        subprocess.run([BUSYBOX, "sh", str(SCRIPT), "stop"], env=self.env, timeout=20)
        if self.background:
            self.background.terminate()
            try:
                self.background.wait(5)
            except subprocess.TimeoutExpired:
                self.background.kill()
        self.tmp.cleanup()

    def service(self, name, body, *flags):
        directory = self.services / name
        directory.mkdir(parents=True, exist_ok=True)
        run = directory / "run"
        run.write_text("#!/bin/sh\n" + body)
        run.chmod(0o755)
        for flag in flags:
            (directory / flag).write_text("")
        return directory

    def step(self):
        result = subprocess.run([BUSYBOX, "sh", str(SCRIPT), "once"], env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.read_status()

    def read_status(self):
        if not self.status.exists():
            return {}
        return dict(line.split("=", 1) for line in self.status.read_text().splitlines())

    def wait_for(self, predicate, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.1)
        return predicate()

    def test_nothing_runs_until_enabled(self):
        marker = self.base / "ran"
        self.service("probe", f'touch "{marker}"\nexec sleep 30\n')
        status = self.step()
        self.assertEqual((status["enabled"], status["state"]), ("0", "disabled"))
        time.sleep(0.5)
        self.assertFalse(marker.exists())

    def test_service_runs_down_stops_and_status_names_only(self):
        (self.local / "enabled").write_text("")
        marker = self.base / "ran"
        directory = self.service("web-extra", f'echo hello\ntouch "{marker}"\nexec sleep 30\n')
        status = self.step()
        self.assertEqual((status["enabled"], status["state"]), ("1", "running"))
        self.assertEqual(status["service.web-extra"], "running")
        self.assertTrue(self.wait_for(marker.exists))
        self.assertTrue(self.wait_for(lambda: "hello" in (self.logs / "web-extra.log").read_text()))
        self.assertNotIn("hello", self.status.read_text(), "status never carries service output")
        (directory / "down").write_text("")
        status = self.step()
        self.assertEqual(status["service.web-extra"], "down")
        pid = (self.run_root / "web-extra/pid")
        self.assertFalse(pid.exists())

    def test_once_service_runs_once_and_records_exit(self):
        (self.local / "enabled").write_text("")
        counter = self.base / "count"
        self.service("startup", f'echo x >> "{counter}"\nexit 0\n', "once")
        self.step()
        self.assertTrue(self.wait_for(lambda: (self.run_root / "startup/rc").exists()))
        status = self.step()
        self.assertEqual(status["service.startup"], "exited:0")
        for _ in range(3):
            self.step()
            time.sleep(0.2)
        self.assertEqual(counter.read_text().count("x"), 1, "a once service is not restarted")

    def test_crashing_service_backs_off_then_gives_up(self):
        (self.local / "enabled").write_text("")
        counter = self.base / "count"
        self.service("crashy", f'echo x >> "{counter}"\nexit 3\n')
        states = []
        for _ in range(40):
            states.append(self.step().get("service.crashy"))
            if states[-1] == "failed":
                break
            time.sleep(0.3)
        self.assertEqual(states[-1], "failed", states)
        self.assertIn("exited:3", states)
        runs = counter.read_text().count("x")
        self.assertEqual(runs, 3, "given up after LOCAL_MAX_FAILURES attempts")
        for _ in range(3):
            self.step()
        self.assertEqual(counter.read_text().count("x"), runs, "a failed service stays stopped")

    def test_disabling_stops_the_whole_process_group(self):
        (self.local / "enabled").write_text("")
        child_pid = self.base / "child.pid"
        self.service("forker", f'sleep 60 &\necho $! > "{child_pid}"\nexec sleep 60\n')
        self.step()
        self.assertTrue(self.wait_for(child_pid.exists))
        child = int(child_pid.read_text())
        os.kill(child, 0)
        (self.local / "enabled").unlink()
        status = self.step()
        self.assertEqual(status["state"], "disabled")
        self.assertTrue(self.wait_for(lambda: not Path(f"/proc/{child}").exists()),
                        "a child the service forked is stopped too")

    def test_invalid_names_and_limit_are_skipped(self):
        (self.local / "enabled").write_text("")
        self.service("Bad Name", "exec sleep 30\n")
        self.service("UPPER", "exec sleep 30\n")
        for index in range(18):
            self.service(f"svc{index:02d}", "exec sleep 30\n")
        status = self.step()
        services = {k for k in status if k.startswith("service.")}
        self.assertEqual(len(services), 16)
        self.assertNotIn("service.UPPER", status)
        self.assertNotIn("service.svc16", status)

    def test_unsafe_root_is_refused(self):
        real = self.base / "elsewhere"
        shutil.move(str(self.local), str(real))
        os.symlink(real, self.local)
        (real / "enabled").write_text("")
        marker = self.base / "ran"
        (real / "services/probe").mkdir(parents=True)
        run = real / "services/probe/run"
        run.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
        run.chmod(0o755)
        status = self.step()
        self.assertEqual(status["state"], "skipped:path-unsafe")
        time.sleep(0.5)
        self.assertFalse(marker.exists())

    def test_log_is_capped(self):
        (self.local / "enabled").write_text("")
        self.env["LOCAL_LOG_MAX_BYTES"] = "4096"
        self.service("chatty", 'i=0\nwhile [ $i -lt 400 ]; do echo 0123456789012345678901234567890123456789; i=$((i+1)); done\nexec sleep 30\n')
        self.step()
        log = self.logs / "chatty.log"
        self.assertTrue(self.wait_for(lambda: log.exists() and log.stat().st_size > 4096))
        self.step()
        self.assertLessEqual(log.stat().st_size, 4096)
        self.assertTrue((self.logs / "chatty.log.1").exists())

    def test_boot_loop_guard_trips_and_resets(self):
        (self.local / "enabled").write_text("")
        (self.local / ".boot-guard").write_text("3\n")
        marker = self.base / "ran"
        self.service("probe", f'touch "{marker}"\nexec sleep 30\n')
        result = subprocess.run([BUSYBOX, "sh", str(SCRIPT), "run"], env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_status()["state"], "skipped:boot-loop-guard")
        self.assertFalse(marker.exists())
        (self.local / ".boot-guard").write_text("1\n")
        self.env["LOCAL_BOOT_GUARD_RESET_SECONDS"] = "1"
        self.background = subprocess.Popen([BUSYBOX, "sh", str(SCRIPT), "run"], env=self.env,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertTrue(self.wait_for(marker.exists))
        self.assertTrue(self.wait_for(lambda: (self.local / ".boot-guard").read_text().strip() == "0"),
                        "surviving the reset window clears the guard")

    def test_live_disable_from_the_running_supervisor(self):
        (self.local / "enabled").write_text("")
        marker = self.base / "ran"
        self.service("probe", f'touch "{marker}"\nexec sleep 30\n')
        self.background = subprocess.Popen([BUSYBOX, "sh", str(SCRIPT), "run"], env=self.env,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertTrue(self.wait_for(lambda: self.read_status().get("service.probe") == "running"))
        pid = int((self.run_root / "probe/pid").read_text())
        (self.local / "enabled").unlink()
        self.assertTrue(self.wait_for(lambda: self.read_status().get("state") == "disabled"))
        self.assertTrue(self.wait_for(lambda: not Path(f"/proc/{pid}").exists()))
        (self.local / "enabled").write_text("")
        self.assertTrue(self.wait_for(lambda: self.read_status().get("service.probe") == "running"),
                        "re-enabling starts services again without a restart")


if __name__ == "__main__":
    unittest.main()

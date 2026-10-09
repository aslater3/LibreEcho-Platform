#!/usr/bin/env python3
"""Contracts for the anonymous active-device ping (telemetry Tier 0).

The sender is exercised under the host busybox with a fake curl that records
its arguments and returns a chosen HTTP status, so the de-duplication, payload
and failure rules are tested without network access.
"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
PING = HERE / "initramfs/libreecho-ping"
INIT = HERE / "initramfs/libreecho-init"
BUSYBOX = shutil.which("busybox") or ""

TAG = "radar-puffin-build-cc238ba-8eb6e5d5bb1f203d-34bf2f0e787f4f6a"
FORBIDDEN_KEYS = {"id", "uuid", "serial", "mac", "hostname", "ip", "ts", "time", "age", "install"}


@unittest.skipUnless(BUSYBOX, "busybox is required")
class ActiveDevicePingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        (self.tmp / "gen/txn-1").mkdir(parents=True)
        (self.tmp / "update").mkdir()
        self.write_identity("radar_puffin", "0.14.0", "dev")
        (self.tmp / "update/current").write_text("txn-1\n")
        (self.tmp / "gen/txn-1/target.manifest").write_text(f"board=radar_puffin\nrelease={TAG}\nversion=0.14.0\n")
        (self.tmp / "channel").write_text("dev\n")
        (self.tmp / "time.status").write_text("state=synchronized\nsynchronized=1\n")
        (self.tmp / "ca.pem").write_text("ca\n")
        self.curl = self.tmp / "curl"
        self.curl_log = self.tmp / "curl.log"
        self.set_http("204")

    def write_identity(self, target: str, version: str, channel: str) -> None:
        (self.tmp / "target").write_text(f"target_id={target}\nrelease_slug=x\n")
        (self.tmp / "update/installed").write_text(
            f"schema=3\nslot=a\nversion={version}\nupdate_channel={channel}\n")

    def set_http(self, code: str) -> None:
        self.curl.write_text(
            "#!/bin/sh\n"
            f"for a in \"$@\"; do printf '%s\\n' \"$a\"; done > '{self.curl_log}'\n"
            f"printf '%s' '{code}'\n")
        self.curl.chmod(0o755)

    def run_ping(self, command: str, now: str = "2026-10-09 12:00:00") -> subprocess.CompletedProcess:
        env = {
            "PATH": os.environ["PATH"], "BB": BUSYBOX,
            "LIBREECHO_PING_STATE": str(self.tmp / "state"),
            "LIBREECHO_PING_TARGET": str(self.tmp / "target"),
            "LIBREECHO_PING_INSTALLED": str(self.tmp / "update/installed"),
            "LIBREECHO_PING_UPDATE_ROOT": str(self.tmp / "update"),
            "LIBREECHO_PING_GENERATIONS": str(self.tmp / "gen"),
            "LIBREECHO_PING_CHANNEL_FILE": str(self.tmp / "channel"),
            "LIBREECHO_TIME_STATUS": str(self.tmp / "time.status"),
            "LIBREECHO_PING_CURL": str(self.curl),
            "LIBREECHO_PING_CA": str(self.tmp / "ca.pem"),
            "LIBREECHO_PING_NOW": now,
        }
        return subprocess.run([BUSYBOX, "sh", str(PING), command], env=env,
                              capture_output=True, text=True, timeout=30)

    def sent(self) -> tuple[dict, list[str]]:
        args = self.curl_log.read_text().splitlines()
        body = json.loads(args[args.index("--data-binary") + 1])
        self.curl_log.unlink()
        return body, args

    def state(self, name: str) -> str:
        path = self.tmp / "state" / name
        return path.read_text().strip() if path.exists() else ""

    def test_dev_payload_is_exact_and_anonymous(self) -> None:
        self.assertEqual(self.run_ping("run").returncode, 0)
        body, args = self.sent()
        self.assertEqual(body, {"v": 1, "hw": "radar", "ver": "0.14.0", "ch": "dev", "build": "cc238ba",
                                "wk": "2026-W41", "mo": "2026-10", "yr": "2026", "w": 1, "m": 1, "y": 1})
        self.assertFalse(FORBIDDEN_KEYS & set(body))
        self.assertEqual(args[args.index("-A") + 1], "libreecho-ping/1")
        self.assertIn("Content-Type: application/json", args)
        self.assertEqual(args[-1], "https://stats.libreecho.org/v1/ping")
        self.assertIn("--proto", args)
        self.assertEqual(args[args.index("--proto") + 1], "=https")

    def test_stable_payload_has_no_build_and_biscuit_maps(self) -> None:
        self.write_identity("biscuit", "0.14.0", "stable")
        self.assertEqual(self.run_ping("run").returncode, 0)
        body, _ = self.sent()
        self.assertEqual(body["hw"], "biscuit")
        self.assertEqual(body["ch"], "stable")
        self.assertNotIn("build", body)

    def test_counts_once_per_period(self) -> None:
        self.assertEqual(self.run_ping("run").returncode, 0)
        self.sent()
        self.assertEqual((self.state("last-week"), self.state("last-month"), self.state("last-year")),
                         ("2026-W41", "2026-10", "2026"))
        # Same week: nothing is sent at all.
        self.assertEqual(self.run_ping("run", "2026-10-11 23:00:00").returncode, 0)
        self.assertFalse(self.curl_log.exists())
        # Next week, same month and year: only w.
        self.run_ping("run", "2026-10-12 08:00:00")
        body, _ = self.sent()
        self.assertEqual((body["wk"], body["w"], body["m"], body["y"]), ("2026-W42", 1, 0, 0))

    def test_month_rollover_inside_a_counted_week(self) -> None:
        # Tue 29 Sep 2026 is W40; Thu 1 Oct is still W40 but a new month.
        self.run_ping("run", "2026-09-29 10:00:00")
        self.sent()
        self.run_ping("run", "2026-10-01 10:00:00")
        body, _ = self.sent()
        self.assertEqual((body["wk"], body["mo"], body["w"], body["m"], body["y"]), ("2026-W40", "2026-10", 0, 1, 0))

    def test_failure_does_not_advance_markers(self) -> None:
        for code in ("000", "500", "404", "429"):
            self.set_http(code)
            self.assertEqual(self.run_ping("run").returncode, 1, code)
            self.assertFalse((self.tmp / "state/last-week").exists(), code)
            self.assertIn("result=failed", (self.tmp / "state/status").read_text())
        self.set_http("204")
        self.run_ping("run")
        body, _ = self.sent()
        self.assertEqual((body["w"], body["m"], body["y"]), (1, 1, 1))

    def test_rejection_keeps_markers_and_backs_off(self) -> None:
        # A 400 (version not yet allow-listed) must not consume the period:
        # once the server accepts the version the device is still counted.
        self.set_http("400")
        self.assertEqual(self.run_ping("run").returncode, 0)
        self.assertEqual(self.state("last-week"), "")
        self.assertIn("result=rejected", (self.tmp / "state/status").read_text())
        self.set_http("204")
        self.run_ping("run")
        body, _ = self.sent()
        self.assertEqual((body["w"], body["m"], body["y"]), (1, 1, 1))
        self.assertEqual(self.state("last-week"), "2026-W41")

    def test_unsynchronised_clock_sends_nothing(self) -> None:
        env_time = self.tmp / "time.status"
        env_time.write_text("state=waiting\nsynchronized=0\n")
        # LIBREECHO_PING_NOW bypasses the clock check, so drive real time here.
        result = self.run_ping("run", now="")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.curl_log.exists())

    def test_unknown_identity_sends_nothing(self) -> None:
        self.write_identity("echo_dot_99", "0.14.0", "stable")
        self.assertEqual(self.run_ping("run").returncode, 1)
        self.assertFalse(self.curl_log.exists())
        self.write_identity("radar_puffin", "", "stable")
        self.assertEqual(self.run_ping("run").returncode, 1)
        self.assertFalse(self.curl_log.exists())
        # A dev build whose tag cannot be read must not send a build-less dev ping.
        self.write_identity("radar_puffin", "0.14.0", "dev")
        (self.tmp / "gen/txn-1/target.manifest").write_text("release=local-build\n")
        self.assertEqual(self.run_ping("run").returncode, 1)
        self.assertFalse(self.curl_log.exists())

    def test_payload_preview_matches_what_is_sent(self) -> None:
        preview = self.run_ping("payload")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.run_ping("run")
        body, _ = self.sent()
        self.assertEqual(json.loads(preview.stdout), body)
        self.assertEqual(json.loads(self.state("next-payload")), body)

    def test_init_starts_sender_only_on_ota_images_in_background(self) -> None:
        init = INIT.read_text()
        self.assertIn("/usr/local/sbin/libreecho-ping loop", init)
        line = next(l for l in init.splitlines() if "libreecho-ping loop" in l)
        self.assertTrue(line.rstrip().endswith("&"), line)
        block = init[init.index('if [ "$IMAGE_PROFILE" = ota ]; then\n    ota_health_confirm_worker'):]
        block = block[:block.index("\nfi\n")]
        self.assertIn("libreecho-ping loop", block)

    def test_sender_is_independent_of_the_updater(self) -> None:
        source = PING.read_text()
        for word in ("libreecho-update", "update-fetch", "check-status", "bootctl"):
            self.assertNotIn(word, source.replace("libreecho-update-fetch never", ""))


if __name__ == "__main__":
    unittest.main()

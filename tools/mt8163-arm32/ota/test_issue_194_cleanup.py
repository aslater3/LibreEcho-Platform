#!/usr/bin/env python3
"""Regression coverage for refused OTA install working-set cleanup."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


FETCH = Path(__file__).resolve().parents[1] / "initramfs/libreecho-update-fetch"


def function_body(source: str, name: str, next_name: str) -> str:
    start = source.index(f"{name}()")
    end = source.index(f"{next_name}()", start)
    return source[start:end]


class RefusedInstallCleanupTests(unittest.TestCase):
    def test_feature_preflight_refusal_reclaims_working_set_and_retry_runs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ota-194-cleanup-") as directory:
            root = Path(directory)
            update = root / "update"
            package = update / "incoming/github-update.ota.tar"
            feature_stage = update / "staging/features"
            quarantine = update / "quarantine-deadbeef.bad"
            for path in (package, quarantine):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"temporary")
            (feature_stage / "tts").mkdir(parents=True)
            (feature_stage / "tts/payload.squashfs").write_bytes(b"payload")
            (update / ".control-resume.123").write_bytes(b"temporary")
            attempts = root / "attempts"
            update_script = root / "update-command"
            update_script.write_text(
                "#!/bin/sh\n"
                f"n=$(cat {attempts} 2>/dev/null || printf 0)\n"
                "n=$((n + 1)); printf '%s\\n' \"$n\" > " + str(attempts) + "\n"
                "[ \"$n\" -gt 1 ] || { printf '%s\\n' ERROR:feature_preflight >&2; exit 1; }\n"
            )
            update_script.chmod(0o755)

            source = FETCH.read_text()
            cleanup = function_body(source, "cleanup_refused_install_artifacts", "candidate_matches_record")
            check = function_body(source, "check_or_install", "watch_updates")
            harness = f"""#!/bin/sh
BB=/bin/busybox
ROOT={root / 'update'}
PACKAGE={package}
PART=$PACKAGE.part
FEATURE_STAGE={feature_stage}
PENDING=$ROOT/pending
INSTALL_LOCK=$ROOT/install.lock
FETCH_LOCK=$ROOT/fetch.lock
FEATURE_TRANSACTION={root / 'feature-transaction'}
UPDATE={update_script}
DATA_ROOT={root / 'data'}
PROFILE={root / 'profile'}
CONFIG={root / 'config'}
CHANNEL_FILE=$ROOT/automatic-updates
PACKAGED_CHANNEL_FILE={root / 'packaged-channel'}
CURL_STDERR={root / 'curl.stderr'}
CURL_HEADERS={root / 'curl.headers'}
RUN_ROOT={root / 'run'}
CHECK_STATUS=$ROOT/check-status
AUTOMATIC=$ROOT/automatic-updates
channel=dev
version=0.14.0
DEV_RELEASE_TAG=dev
DEV_OTA_SHA256=deadbeef
FETCH_LOCK_HELD=0
INSTALL_LOCK_HELD=0
{cleanup}
require_environment() {{ :; }}
fetch_lock() {{ :; }}
install_lock() {{ :; }}
install_unlock() {{ :; }}
seed_channel() {{ :; }}
validate_source() {{ :; }}
prepare_https_client() {{ :; }}
resolve_dev_release() {{ :; }}
download_and_inspect() {{ version=0.14.0; :; }}
download_feature_assets() {{ :; }}
check_status_write() {{ :; }}
check_status_write_candidate() {{ :; }}
check_value_from_file() {{ [ \"$2\" = format ] && printf 'libreecho-ota-v2'; }}
candidate_matches_record() {{ return 1; }}
{check}
check_or_install install
first=$?
check_or_install install
second=$?
printf 'first=%s second=%s\\n' \"$first\" \"$second\"
"""
            script = root / "harness.sh"
            script.write_text(harness)
            script.chmod(0o755)
            result = subprocess.run(["/bin/sh", str(script)], text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ERROR:feature_preflight", result.stderr)
            self.assertIn("first=1 second=0", result.stdout)
            self.assertFalse(feature_stage.exists(), list(update.rglob("*")))
            self.assertFalse(package.exists())
            self.assertFalse(quarantine.exists())
            self.assertFalse((update / ".control-resume.123").exists())

    def test_cleanup_leaves_durable_transaction_evidence_intact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ota-194-evidence-") as directory:
            root = Path(directory)
            update = root / "update"
            feature_stage = update / "staging/features"
            package = update / "incoming/github-update.ota.tar"
            feature_stage.mkdir(parents=True)
            package.parent.mkdir(parents=True)
            package.write_bytes(b"package")
            (feature_stage / "manifest").write_text("signed-evidence\n")
            journal = update / "feature-commit"
            journal.write_text("transaction_id=authoritative\n")
            source = FETCH.read_text()
            cleanup = function_body(source, "cleanup_refused_install_artifacts", "candidate_matches_record")
            script = root / "evidence.sh"
            script.write_text(
                "#!/bin/sh\n"
                f"BB=/bin/busybox\nROOT={update}\nPENDING=$ROOT/pending\n"
                f"PACKAGE={package}\nPART=$PACKAGE.part\nFEATURE_STAGE={feature_stage}\n"
                f"CURL_HEADERS={root / 'headers'}\nCURL_STDERR={root / 'stderr'}\n"
                f"{cleanup}\ncleanup_refused_install_artifacts\n"
            )
            script.chmod(0o755)
            result = subprocess.run(["/bin/sh", str(script)], text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(journal.exists())
            self.assertTrue(feature_stage.exists())
            self.assertTrue(package.exists())


if __name__ == "__main__":
    unittest.main()

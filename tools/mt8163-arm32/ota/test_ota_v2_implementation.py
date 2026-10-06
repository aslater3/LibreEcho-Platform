#!/usr/bin/env python3
"""Fresh host contracts for the reboot-bound OTA v2 implementation."""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
import sys
import shlex
import shutil
import signal
import time
from pathlib import Path
from nacl.signing import SigningKey

OTA = Path(__file__).resolve().parent
TOOLS = OTA.parent
sys.path.insert(0, str(OTA))
TRANSACTION = TOOLS / "initramfs/libreecho-feature-transaction"
INIT = TOOLS / "initramfs/libreecho-init"
BOOT_SIZE = 16 * 1024 * 1024
KEY = bytes(range(32))
FEATURE_IDS = ("airplay2", "tts", "wakeword", "stt", "assistant")

def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=False, **kwargs)

def feature(feature_id: str, action: str) -> dict[str, object]:
    value: dict[str, object] = {
        "feature_id": feature_id,
        "action": action,
        "activation": "reboot",
        "base_payload_sha256": "a" * 64,
        "base_manifest_sha256": "b" * 64,
        "daemon_path": {
            "airplay2": "usr/local/sbin/libreecho-audio-engine",
            "tts": "usr/local/sbin/libreecho-ttsd",
            "wakeword": "usr/local/sbin/libreecho-waked",
            "stt": "usr/local/sbin/libreecho-sttd",
            "assistant": "usr/local/sbin/libreecho-agentd",
        }[feature_id],
        "daemon_sha256": "c" * 64,
        "release": "0.13.11",
        "source_commit": "0123456789abcdef0123456789abcdef01234567",
    }
    if action in {"runtime", "replace"}:
        suffix = "runtime.squashfs" if action == "runtime" else "payload.squashfs"
        manifest_suffix = "runtime-manifest.json" if action == "runtime" else "manifest.json"
        prefix = f"libreecho-radar-puffin-0.13.11-{feature_id}"
        value.update({
            "asset": prefix + "." + suffix,
            "size": 5,
            "sha256": "d" * 64,
            "manifest_asset": prefix + "." + manifest_suffix,
            "manifest_size": 7,
            "manifest_sha256": "e" * 64,
        })
    return value

def manifest(features: list[dict[str, object]]) -> dict[str, object]:
    supplied = {str(record["feature_id"]): record for record in features}
    records = [supplied.get(feature_id, feature(feature_id, "preserve")) for feature_id in FEATURE_IDS]
    return {
        "format": "libreecho-ota-v2",
        "manifest_version": 1,
        "board": "radar_puffin",
        "soc": "mt8163",
        "architecture": "armv7",
        "image_profile": "ota",
        "transaction_type": "system",
        "transaction_id": "txn-0.13.11-test",
        "version": "0.13.11",
        "update_channel": "stable",
        "service_profile": "production",
        "feature_policy": "preserve",
        "minimum_updater_schema": 2,
        "feature_asset_base": "github-release-channel",
        "commit_policy": "after-slot-confirm",
        "feature_ids": ",".join(str(f["feature_id"]) for f in records),
        "boot_filename": "boot.img",
        "boot_size": BOOT_SIZE,
        "boot_sha256": "f" * 64,
        "features": records,
    }

class ManifestAndBundleTests(unittest.TestCase):
    def test_signed_policy_profile_allowlist_rejects_invalid_pairs(self) -> None:
        from feature_manifest import ContractError, serialize_manifest
        for policy, profile in (
            ("exclude", "production"),
            ("redistributable", "diagnostic"),
            ("community-noncommercial", "diagnostic"),
            ("unknown", "production"),
            ("preserve", "unknown"),
        ):
            with self.subTest(policy=policy, profile=profile):
                bad = manifest([])
                bad["feature_policy"] = policy
                bad["service_profile"] = profile
                with self.assertRaises(ContractError):
                    serialize_manifest(bad)

    def test_manifest_rejects_partial_feature_graph_in_memory_and_wire_forms(self) -> None:
        from feature_manifest import ContractError, parse_manifest, serialize_manifest

        partial = manifest([feature("assistant", "preserve")])
        partial["features"] = partial["features"][-1:]
        partial["feature_ids"] = "assistant"
        with self.assertRaises(ContractError):
            serialize_manifest(partial)

        full_raw = serialize_manifest(manifest([feature("assistant", "preserve")]))
        partial_raw = b"".join(
            (b"feature_ids=assistant\n" if line.startswith(b"feature_ids=") else line)
            for line in full_raw.splitlines(keepends=True)
            if not line.startswith((b"feature_airplay2_", b"feature_tts_", b"feature_wakeword_", b"feature_stt_"))
        )
        with self.assertRaises(ContractError):
            parse_manifest(partial_raw)

    def test_reboot_only_manifest_round_trip_supports_preserve_runtime_replace(self) -> None:
        from feature_manifest import parse_manifest, serialize_manifest
        records = [feature("airplay2", "preserve"), feature("stt", "runtime"), feature("assistant", "replace")]
        obj = manifest(records)
        raw = serialize_manifest(obj)
        parsed = parse_manifest(raw)
        self.assertEqual(parsed["transaction_type"], "system")
        self.assertEqual(
            [r["action"] for r in parsed["features"]],
            ["preserve", "preserve", "preserve", "runtime", "replace"],
        )

    def test_v2_rejects_hot_hotfix_remove_and_missing_boot_before_side_effects(self) -> None:
        from feature_manifest import ContractError, serialize_manifest
        for mutation in (
            {"transaction_type": "feature-hotfix"},
            {"features": [dict(feature("assistant", "runtime"), activation="hot")]},
            {"features": [feature("assistant", "remove")]},
            {"boot_filename": None, "boot_size": None, "boot_sha256": None},
        ):
            with self.subTest(mutation=mutation):
                bad = manifest([feature("assistant", "runtime")])
                if "features" in mutation:
                    bad["features"][-1] = mutation["features"][0]
                else:
                    bad.update(mutation)
                if mutation.get("transaction_type") == "feature-hotfix":
                    bad["features"][0]["activation"] = "hot"
                with self.assertRaises(ContractError):
                    serialize_manifest(bad)

    def test_control_tar_has_only_manifest_signature_and_boot(self) -> None:
        from feature_manifest import build_control_tar, serialize_manifest
        from nacl.signing import SigningKey
        boot = b"ANDROID!" + bytes(BOOT_SIZE - 8)
        records = [feature("assistant", "runtime")]
        obj = manifest(records)
        obj["boot_sha256"] = hashlib.sha256(boot).hexdigest()
        key = SigningKey(KEY)
        data = build_control_tar(obj, boot, key)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            self.assertEqual(archive.getnames(), ["manifest", "manifest.sig", "boot.img"])
            self.assertEqual(archive.extractfile("boot.img").read(), boot)

    def test_make_ota_bundle_v2_cli_binds_external_feature_plan(self) -> None:
        from nacl.signing import SigningKey
        with tempfile.TemporaryDirectory(prefix="ota-v2-builder-") as directory:
            root = Path(directory)
            boot = root / "boot.img"
            boot.write_bytes(b"ANDROID!" + bytes(BOOT_SIZE - 8))
            signing = SigningKey(KEY)
            key = root / "signing.hex"; key.write_text(KEY.hex())
            public = root / "public.hex"; public.write_text(signing.verify_key.encode().hex())
            feature_record = feature("assistant", "runtime")
            plan_records = [feature(feature_id, "preserve") for feature_id in FEATURE_IDS[:-1]] + [feature_record]
            plan = root / "feature-plan.json"; plan.write_text(json.dumps({"features": plan_records}))
            build = root / "build.json"; build.write_text(json.dumps({
                "output": {"sha256": hashlib.sha256(boot.read_bytes()).hexdigest(), "size": BOOT_SIZE},
                "image_profile": "ota", "service_profile": "production", "feature_policy": "preserve", "update_channel": "stable",
            }))
            output = root / "update.ota.tar"
            result = run([sys.executable, str(OTA / "make_ota_bundle.py"), "--format", "v2", "--boot-image", str(boot),
                          "--build-manifest", str(build), "--feature-plan", str(plan), "--version", "0.13.11",
                          "--signing-key", str(key), "--public-key", str(public), "--service-profile", "production",
                          "--feature-policy", "preserve", "--update-channel", "stable", "--output", str(output)])
            self.assertEqual(result.returncode, 0, result.stderr)
            with tarfile.open(output, "r:") as archive:
                self.assertEqual(archive.getnames(), ["manifest", "manifest.sig", "boot.img"])


def shell_function_block(source: str, first: str, end_before: str) -> str:
    start = source.index(first)
    return source[start:source.index(f"{end_before}()", start)]


class StaleDownloadRecoveryTests(unittest.TestCase):
    def test_stale_manifest_recovery_never_signals_persisted_pids(self) -> None:
        """A PID saved under /data before a power loss names nothing after it.

        The manifest outlives the boot but the PID namespace does not: after a
        reboot the same number can belong to any unrelated service. Recovery
        must remove the inert artifacts and leave every process alone.
        """
        source = (TOOLS / "initramfs/libreecho-update-fetch").read_text()
        helpers = shell_function_block(source, "MAX_STALE_MANIFESTS=", "download_signal")
        with tempfile.TemporaryDirectory(prefix="ota-stale-pid-") as directory:
            root = Path(directory) / "update"
            root.mkdir()
            harness = Path(directory) / "harness.sh"
            harness.write_text(
                "BB=/bin/busybox\n"
                f"ROOT={shlex.quote(str(root))}\n"
                f"CURL_HEADERS={shlex.quote(str(Path(directory) / 'headers'))}\n"
                f"CURL_STDERR={shlex.quote(str(Path(directory) / 'stderr'))}\n"
                + helpers
                + "recover_stale_download_artifacts\n"
            )
            bystander = subprocess.Popen(["sleep", "60"])
            try:
                artifact = root / ".control-fresh.424250"
                artifact.write_text("stale")
                manifest_path = root / ".control-owned.424250"
                manifest_path.write_text(f"{artifact}\npid:{bystander.pid}\n")
                result = run(["/bin/busybox", "sh", str(harness)], timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                time.sleep(0.2)
                self.assertIsNone(bystander.poll(), "stale recovery signalled an unrelated process")
                self.assertFalse(artifact.exists())
                self.assertFalse(manifest_path.exists())
            finally:
                bystander.kill()
                bystander.wait(timeout=10)


class UserdataCleanupSymlinkTests(unittest.TestCase):
    def test_symlinked_feature_directory_is_rejected_before_any_deletion(self) -> None:
        """Legacy residue removal must not follow a feature-directory symlink.

        The contract check already refuses a symlinked feature directory, but
        the residue loop ran first and its rm -rf resolved through the link.
        """
        with tempfile.TemporaryDirectory(prefix="cleanup-feature-link-") as directory:
            root = Path(directory)
            data = root / "data"
            features = data / "libreecho/features"
            features.mkdir(parents=True)
            outside = root / "outside"
            (outside / "staging").mkdir(parents=True)
            (outside / "staging/must-survive").write_text("keep")
            (outside / "payload.squashfs.previous").write_text("keep")
            (features / "linked").symlink_to(outside, target_is_directory=True)
            result = run(
                ["/bin/sh", str(TOOLS / "initramfs/libreecho-data-cleanup")],
                env={**os.environ, "LIBREECHO_DATA_TEST_MODE": "1", "DATA_ROOT": str(data)},
                timeout=30,
            )
            output = result.stdout + result.stderr
            self.assertTrue((outside / "staging/must-survive").exists(), output)
            self.assertTrue((outside / "payload.squashfs.previous").exists(), output)
            self.assertTrue((features / "linked").is_symlink())
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("DATA_CLEANUP_UNKNOWN=libreecho/features/linked", output)
            self.assertNotIn("DATA_CLEANUP_REMOVED=libreecho/features/linked", output)


if __name__ == "__main__":
    unittest.main()

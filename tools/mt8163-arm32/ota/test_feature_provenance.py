#!/usr/bin/env python3
"""Signed-fixture tests for the read-only feature provenance command."""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from nacl.signing import SigningKey

OTA = Path(__file__).resolve().parent
TOOLS = OTA.parent
TRANSACTION = TOOLS / "initramfs/libreecho-feature-transaction"
BOOT_SIZE = 16 * 1024 * 1024
FEATURE_IDS = ("airplay2", "tts", "wakeword", "stt", "assistant")
DAEMONS = {
    "airplay2": "usr/local/sbin/libreecho-audio-engine",
    "tts": "usr/local/sbin/libreecho-ttsd",
    "wakeword": "usr/local/sbin/libreecho-waked",
    "stt": "usr/local/sbin/libreecho-sttd",
    "assistant": "usr/local/sbin/libreecho-agentd",
}


def run(command: list[str], env: dict[str, str], timeout: int = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, env=env, text=True, capture_output=True, timeout=timeout, check=False)


def snapshot_tree(root: Path) -> dict[Path, tuple[str, bytes | str | None]]:
    """Capture file bytes and non-regular object identity without following links."""
    snapshot: dict[Path, tuple[str, bytes | str | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            snapshot[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            snapshot[relative] = ("directory", None)
        else:
            snapshot[relative] = ("other", None)
    return snapshot


def sign(key: SigningKey, data: bytes) -> bytes:
    return key.sign(data).signature.hex().encode("ascii") + b"\n"


def write_verifier(root: Path, public_key: Path, key: SigningKey) -> Path:
    verifier = root / "verify-ed25519.py"
    verifier.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from nacl.signing import VerifyKey\n"
        "try:\n"
        "    VerifyKey(bytes.fromhex(Path(sys.argv[1]).read_text().strip())).verify(\n"
        "        Path(sys.argv[2]).read_bytes(), bytes.fromhex(Path(sys.argv[3]).read_text().strip()))\n"
        "except Exception:\n"
        "    raise SystemExit(1)\n"
    )
    verifier.chmod(0o755)
    public_key.write_text(key.verify_key.encode().hex() + "\n")
    return verifier


def shell_fixture(root: Path, env: dict[str, str]) -> Path:
    values = {
        "BB=/bin/busybox": "BB=/bin/busybox",
        "ROOT=/data/libreecho/update": f"ROOT={root / 'data/libreecho/update'}",
        "FEATURES=/data/libreecho/features": f"FEATURES={root / 'data/libreecho/features'}",
        "BCB_FILE=$ROOT/staging/bootctl.readback": f"BCB_FILE={root / 'data/libreecho/update/staging/bootctl.readback'}",
        "PROC_ROOT=/proc": f"PROC_ROOT={root / 'proc'}",
        "VAR_RUN_ROOT=/var/run": f"VAR_RUN_ROOT={root / 'var/run'}",
        "ETC_ROOT=/etc": f"ETC_ROOT={root / 'etc'}",
        "RUN_ROOT=/run": f"RUN_ROOT={root / 'run'}",
        "CONFIG_FILE=/data/libreecho/config/web-config.json": f"CONFIG_FILE={root / 'config.json'}",
        "VERIFY=/usr/local/libexec/libreecho-update-verify": f"VERIFY={env['VERIFY']}",
        "PUBLIC_KEY=/etc/libreecho/ota-public-key.hex": f"PUBLIC_KEY={env['PUBLIC_KEY']}",
        "TRANSACTION_SLOT_FILE=$ROOT/transaction-slot": f"TRANSACTION_SLOT_FILE={root / 'data/libreecho/update/transaction-slot'}",
    }
    text = TRANSACTION.read_text()
    for old, new in values.items():
        if old not in text:
            raise AssertionError(f"fixture anchor missing: {old}")
        text = text.replace(old, new)
    fixture = root / "libreecho-feature-transaction"
    fixture.write_text(text)
    fixture.chmod(0o755)
    return fixture


class ProvenanceFixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="feature-provenance-")
        self.root = Path(self.tmp.name)
        self.update = self.root / "data/libreecho/update"
        self.features = self.root / "data/libreecho/features"
        self.run_root = self.root / "run"
        for feature_id in FEATURE_IDS:
            (self.features / feature_id).mkdir(parents=True)
            (self.run_root / "libreecho/features" / feature_id / "root").mkdir(parents=True)
        self.update.mkdir(parents=True)
        self.key = SigningKey.generate()
        self.public_key = self.root / "public-key.hex"
        self.verifier = write_verifier(self.root, self.public_key, self.key)
        self.env = os.environ.copy()
        self.env.update({
            "LIBREECHO_TRANSACTION_TEST_MODE": "1",
            "VERIFY": str(self.verifier),
            "PUBLIC_KEY": str(self.public_key),
        })
        self.fixture = shell_fixture(self.root, self.env)
        self.build_state()

    def close(self) -> None:
        self.tmp.cleanup()

    def build_state(self) -> None:
        records: list[dict[str, object]] = []
        for feature_id in FEATURE_IDS:
            base_payload = f"{feature_id}-base-payload".encode()
            base_manifest = f"{feature_id}-base-manifest".encode()
            directory = self.features / feature_id
            (directory / "payload.squashfs").write_bytes(base_payload)
            (directory / "manifest.json").write_bytes(base_manifest)
            daemon = f"{feature_id}-daemon".encode()
            effective = self.run_root / "libreecho/features" / feature_id / "root" / DAEMONS[feature_id]
            effective.parent.mkdir(parents=True, exist_ok=True)
            effective.write_bytes(daemon)
            action = {"tts": "replace", "wakeword": "runtime"}.get(feature_id, "preserve")
            release = "0.13.14"
            record: dict[str, object] = {
                "feature_id": feature_id,
                "action": action,
                "activation": "reboot",
                "base_payload_sha256": hashlib.sha256(base_payload).hexdigest(),
                "base_manifest_sha256": hashlib.sha256(base_manifest).hexdigest(),
                "daemon_path": DAEMONS[feature_id],
                "daemon_sha256": hashlib.sha256(daemon).hexdigest(),
                "release": release,
                "source_commit": "0123456789abcdef0123456789abcdef01234567",
            }
            if action == "replace":
                payload = f"{feature_id}-replacement".encode()
                metadata = f"{feature_id}-replacement-manifest".encode()
                (directory / "payload.squashfs").write_bytes(payload)
                (directory / "manifest.json").write_bytes(metadata)
                prefix = f"libreecho-radar-puffin-{release}-{feature_id}"
                record.update({
                    "asset": prefix + ".payload.squashfs",
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "manifest_asset": prefix + ".manifest.json",
                    "manifest_size": len(metadata),
                    "manifest_sha256": hashlib.sha256(metadata).hexdigest(),
                })
            elif action == "runtime":
                runtime = f"{feature_id}-runtime".encode()
                runtime_metadata = f"{feature_id}-runtime-manifest".encode()
                (directory / "runtime.squashfs").write_bytes(runtime)
                (directory / "runtime-manifest.json").write_bytes(runtime_metadata)
                prefix = f"libreecho-radar-puffin-{release}-{feature_id}"
                record.update({
                    "asset": prefix + ".runtime.squashfs",
                    "size": len(runtime),
                    "sha256": hashlib.sha256(runtime).hexdigest(),
                    "manifest_asset": prefix + ".runtime-manifest.json",
                    "manifest_size": len(runtime_metadata),
                    "manifest_sha256": hashlib.sha256(runtime_metadata).hexdigest(),
                })
            records.append(record)
        current = self.make_manifest(records, "txn-provenance-current")
        self.current_manifest = current
        self.committed = self.update / "committed-manifest"
        self.signature = self.update / "committed-manifest.sig"
        self.committed.write_bytes(current)
        self.signature.write_bytes(sign(self.key, current))
        # Runtime generations retain a signed authority pair.  The current
        # wakeword generation is authorized by the current committed values.
        wakeword_authority = self.update / "committed-runtime-wakeword.manifest"
        wakeword_authority.write_bytes(current)
        (self.update / "committed-runtime-wakeword.sig").write_bytes(sign(self.key, current))
        # The retained runtime for stt is authorized by an older signed runtime
        # generation while the current committed manifest preserves that base.
        stt = next(record for record in records if record["feature_id"] == "stt")
        inherited = dict(stt)
        inherited["action"] = "runtime"
        release = "0.13.13"
        inherited["release"] = release
        prefix = f"libreecho-radar-puffin-{release}-stt"
        runtime = b"stt-inherited-runtime"
        runtime_metadata = b"stt-inherited-runtime-manifest"
        inherited.update({
            "asset": prefix + ".runtime.squashfs", "size": len(runtime),
            "sha256": hashlib.sha256(runtime).hexdigest(),
            "manifest_asset": prefix + ".runtime-manifest.json", "manifest_size": len(runtime_metadata),
            "manifest_sha256": hashlib.sha256(runtime_metadata).hexdigest(),
        })
        stt_dir = self.features / "stt"
        (stt_dir / "runtime.squashfs").write_bytes(runtime)
        (stt_dir / "runtime-manifest.json").write_bytes(runtime_metadata)
        authority_records = [inherited if record["feature_id"] == "stt" else record for record in records]
        authority = self.make_manifest(authority_records, "txn-provenance-runtime-stt")
        (self.update / "committed-runtime-stt.manifest").write_bytes(authority)
        (self.update / "committed-runtime-stt.sig").write_bytes(sign(self.key, authority))
        installed = (
            "schema=2\n"
            "transaction_id=txn-provenance-current\n"
            "version=0.13.14\n"
            "slot=b\n"
            f"manifest_sha256={hashlib.sha256(current).hexdigest()}\n"
            f"manifest_sig_sha256={hashlib.sha256(self.signature.read_bytes()).hexdigest()}\n"
            "feature_ids=airplay2,tts,wakeword,stt,assistant\n"
            "phase=installed\n"
        )
        (self.update / "installed").write_text(installed)

    def make_manifest(self, records: list[dict[str, object]], transaction_id: str) -> bytes:
        top = [
            ("format", "libreecho-ota-v2"), ("manifest_version", "1"), ("board", "radar_puffin"),
            ("soc", "mt8163"), ("architecture", "armv7"), ("image_profile", "ota"),
            ("transaction_type", "system"), ("transaction_id", transaction_id), ("version", "0.13.14"),
            ("update_channel", "stable"), ("service_profile", "production"), ("feature_policy", "preserve"),
            ("minimum_updater_schema", "2"), ("feature_asset_base", "github-release-channel"),
            ("commit_policy", "after-slot-confirm"), ("boot_filename", "boot.img"),
            ("boot_size", str(BOOT_SIZE)), ("boot_sha256", "f" * 64),
            ("feature_ids", ",".join(FEATURE_IDS)),
        ]
        output = list(top)
        for record in records:
            prefix = "feature_" + str(record["feature_id"]) + "_"
            for key in ("action", "activation", "base_payload_sha256", "base_manifest_sha256", "daemon_path", "daemon_sha256", "release", "source_commit"):
                output.append((prefix + key, str(record[key])))
            if record["action"] != "preserve":
                for key in ("asset", "size", "sha256", "manifest_asset", "manifest_size", "manifest_sha256"):
                    output.append((prefix + key, str(record[key])))
        return "".join(f"{key}={value}\n" for key, value in output).encode()

    def replace_base_metadata(self, feature_id: str, data: bytes) -> None:
        metadata = self.features / feature_id / "manifest.json"
        old_hash = hashlib.sha256(metadata.read_bytes()).hexdigest()
        metadata.write_bytes(data)
        new_hash = hashlib.sha256(data).hexdigest()
        raw = self.committed.read_bytes()
        raw = raw.replace(
            f"feature_{feature_id}_base_manifest_sha256={old_hash}".encode(),
            f"feature_{feature_id}_base_manifest_sha256={new_hash}".encode(),
        )
        old_manifest_hash = hashlib.sha256(self.committed.read_bytes()).hexdigest()
        old_signature_hash = hashlib.sha256(self.signature.read_bytes()).hexdigest()
        self.committed.write_bytes(raw)
        self.signature.write_bytes(sign(self.key, raw))
        wakeword_authority = self.update / "committed-runtime-wakeword.manifest"
        wakeword_authority.write_bytes(raw)
        (self.update / "committed-runtime-wakeword.sig").write_bytes(sign(self.key, raw))
        installed = (self.update / "installed").read_text()
        installed = installed.replace(
            f"manifest_sha256={old_manifest_hash}",
            f"manifest_sha256={hashlib.sha256(raw).hexdigest()}",
        )
        installed = installed.replace(
            f"manifest_sig_sha256={old_signature_hash}",
            f"manifest_sig_sha256={hashlib.sha256(self.signature.read_bytes()).hexdigest()}",
        )
        (self.update / "installed").write_text(installed)


class ProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ProvenanceFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def invoke(self) -> subprocess.CompletedProcess[str]:
        return run([str(self.fixture.fixture), "provenance"], self.fixture.env)

    def test_signed_preserve_replace_runtime_and_inherited_runtime_contract(self) -> None:
        before = snapshot_tree(self.fixture.root)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(result.stdout.encode()), len(result.stdout))
        self.assertEqual(len(lines), 6 + 5 * 9)
        self.assertEqual(len(lines), len(set(line.split("=", 1)[0] for line in lines)))
        self.assertEqual(lines[0], "schema=libreecho-feature-provenance-v1")
        self.assertIn("feature_tts_action=replace", lines)
        self.assertIn("feature_wakeword_kind=runtime", lines)
        self.assertIn("feature_stt_release=0.13.13", lines)
        self.assertIn("feature_stt_source_commit=0123456789abcdef0123456789abcdef01234567", lines)
        self.assertIn("feature_airplay2_runtime_sha256=none", lines)
        self.assertIn("feature_stt_runtime_manifest_sha256=" + hashlib.sha256(b"stt-inherited-runtime-manifest").hexdigest(), lines)
        after = snapshot_tree(self.fixture.root)
        self.assertEqual(before, after)

    def test_current_runtime_rejects_an_older_signed_authority_and_runtime(self) -> None:
        older_runtime = b"wakeword-older-runtime"
        older_runtime_manifest = b"wakeword-older-runtime-manifest"
        authority = self.fixture.current_manifest
        replacements = {
            "transaction_id": ("txn-provenance-current", "txn-provenance-runtime-wakeword"),
            "feature_wakeword_release": ("0.13.14", "0.13.13"),
            "feature_wakeword_asset": (
                "libreecho-radar-puffin-0.13.14-wakeword.runtime.squashfs",
                "libreecho-radar-puffin-0.13.13-wakeword.runtime.squashfs",
            ),
            "feature_wakeword_size": (str(len(b"wakeword-runtime")), str(len(older_runtime))),
            "feature_wakeword_sha256": (
                hashlib.sha256(b"wakeword-runtime").hexdigest(),
                hashlib.sha256(older_runtime).hexdigest(),
            ),
            "feature_wakeword_manifest_asset": (
                "libreecho-radar-puffin-0.13.14-wakeword.runtime-manifest.json",
                "libreecho-radar-puffin-0.13.13-wakeword.runtime-manifest.json",
            ),
            "feature_wakeword_manifest_size": (
                str(len(b"wakeword-runtime-manifest")),
                str(len(older_runtime_manifest)),
            ),
            "feature_wakeword_manifest_sha256": (
                hashlib.sha256(b"wakeword-runtime-manifest").hexdigest(),
                hashlib.sha256(older_runtime_manifest).hexdigest(),
            ),
        }
        for key, (old, new) in replacements.items():
            authority = authority.replace(f"{key}={old}\n".encode(), f"{key}={new}\n".encode())
        (self.fixture.features / "wakeword/runtime.squashfs").write_bytes(older_runtime)
        (self.fixture.features / "wakeword/runtime-manifest.json").write_bytes(older_runtime_manifest)
        authority_path = self.fixture.update / "committed-runtime-wakeword.manifest"
        authority_path.write_bytes(authority)
        (self.fixture.update / "committed-runtime-wakeword.sig").write_bytes(sign(self.fixture.key, authority))

        result = self.invoke()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_current_runtime_authority_is_accepted(self) -> None:
        result = self.invoke()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "feature_wakeword_runtime_sha256=" + hashlib.sha256(b"wakeword-runtime").hexdigest(),
            result.stdout.splitlines(),
        )

    def test_older_preserved_runtime_authority_is_accepted(self) -> None:
        result = self.invoke()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("feature_stt_release=0.13.13", result.stdout.splitlines())
        self.assertIn(
            "feature_stt_runtime_sha256=" + hashlib.sha256(b"stt-inherited-runtime").hexdigest(),
            result.stdout.splitlines(),
        )

    def test_feature_metadata_accepts_256k_bound_and_rejects_one_byte_over(self) -> None:
        large = b'{"payload":"' + (b"x" * 200000) + b'"}'
        self.fixture.replace_base_metadata("airplay2", large)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("feature_airplay2_manifest_sha256=" + hashlib.sha256(large).hexdigest(), result.stdout.splitlines())

        self.fixture.close()
        self.fixture = ProvenanceFixture()
        oversized = b'{"payload":"' + (b"x" * 262200) + b'"}'
        self.fixture.replace_base_metadata("airplay2", oversized)
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("provenance-airplay2-manifest-too-large", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_tampered_or_missing_signature_has_no_success_stdout(self) -> None:
        for missing in (False, True):
            with self.subTest(missing=missing):
                if missing:
                    self.fixture.signature.unlink()
                else:
                    self.fixture.signature.write_text("0" * 128 + "\n")
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_installed_id_mismatch_and_stale_bytes_fail_closed(self) -> None:
        original = (self.fixture.update / "installed").read_text()
        for name, mutation, reason in (
            ("transaction-id", lambda: (self.fixture.update / "installed").write_text(original.replace("txn-provenance-current", "txn-provenance-other")), "provenance-installed-transaction-mismatch"),
            ("payload", lambda: (self.fixture.features / "airplay2/payload.squashfs").write_bytes(b"stale"), "provenance-airplay2-payload-mismatch"),
            ("manifest", lambda: (self.fixture.features / "airplay2/manifest.json").write_bytes(b"stale"), "provenance-airplay2-manifest-mismatch"),
            ("daemon", lambda: (self.fixture.run_root / "libreecho/features/airplay2/root" / DAEMONS["airplay2"]).write_bytes(b"stale"), "provenance-airplay2-daemon-mismatch"),
        ):
            with self.subTest(name=name):
                mutation()
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn(reason, result.stderr)
                self.fixture.close()
                self.fixture = ProvenanceFixture()
                original = (self.fixture.update / "installed").read_text()

    def test_partial_runtime_fails_closed(self) -> None:
        for name, mutation in (
            ("partial-runtime", lambda: (self.fixture.features / "stt/runtime-manifest.json").unlink()),
            ("tampered-runtime-authority", lambda: (self.fixture.update / "committed-runtime-stt.sig").write_text("0" * 128 + "\n")),
            ("missing-runtime-authority", lambda: (self.fixture.update / "committed-runtime-stt.sig").unlink()),
            ("replace-runtime", lambda: (self.fixture.features / "tts/runtime.squashfs").write_bytes(b"obsolete")),
            ("replace-runtime-authority", lambda: (
                (self.fixture.update / "committed-runtime-tts.manifest").write_bytes(self.fixture.committed.read_bytes()),
                (self.fixture.update / "committed-runtime-tts.sig").write_bytes(sign(self.fixture.key, self.fixture.committed.read_bytes())),
            )),
        ):
            with self.subTest(name=name):
                mutation()
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.fixture.close()
                self.fixture = ProvenanceFixture()

    def test_malformed_oversized_fifo_and_symlink_installed_records_fail_without_blocking(self) -> None:
        installed = self.fixture.update / "installed"
        valid = installed.read_text()
        cases = {
            "malformed": lambda: installed.write_text(valid + "unknown=1\n"),
            "oversized": lambda: installed.write_text(valid + "x=" + "a" * 8200 + "\n"),
            "fifo": lambda: (installed.unlink(), os.mkfifo(installed)),
            "symlink": lambda: (installed.unlink(), installed.symlink_to(self.fixture.root / "outside")),
        }
        for name, mutation in cases.items():
            with self.subTest(name=name):
                mutation()
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.fixture.close()
                self.fixture = ProvenanceFixture()
                installed = self.fixture.update / "installed"
                valid = installed.read_text()

    def test_pending_or_feature_commit_is_rejected_without_writes(self) -> None:
        for name in ("pending", "feature-commit"):
            with self.subTest(name=name):
                (self.fixture.update / name).write_text("unexpected\n")
                before = {path: path.read_bytes() for path in self.fixture.update.iterdir() if path.is_file()}
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                after = {path: path.read_bytes() for path in self.fixture.update.iterdir() if path.is_file()}
                self.assertEqual(before, after)
                (self.fixture.update / name).unlink()


if __name__ == "__main__":
    unittest.main()

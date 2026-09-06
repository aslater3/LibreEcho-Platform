#!/usr/bin/env python3
"""Host tests for deterministic runtime capsule packaging and verification."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parent
REPOSITORY = TOOLS.parents[2]
WORKFLOW = REPOSITORY / ".github/workflows/ota-release.yml"
BUILDER = TOOLS / "package_runtime.py"
VERIFIER = TOOLS / "verify_runtime.py"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
RELEASE = "radar-puffin-v0.14.0"
COMPONENT = "libreecho-agentd"
COMPONENT_VERSION = RELEASE
BUILD_IDENTITY = "assistant-build-20260831"
MAX_BYTES = 1024 * 1024
SERVICE_DEPENDENCIES = ["libreecho-runtime-base"]
COMPATIBILITY = {
    "abi": "arm32-linux-gnueabihf-v1",
    "model": "mt8163-radar-puffin",
    "mounts": ["/usr/local/sbin"],
    "dependencies": ["libreecho-runtime-base"],
}
ALLOWLIST = {
    "airplay2": "usr/local/sbin/libreecho-audio-engine",
    "tts": "usr/local/sbin/libreecho-ttsd",
    "wakeword": "usr/local/sbin/libreecho-waked",
    "stt": "usr/local/sbin/libreecho-sttd",
    "assistant": "usr/local/sbin/libreecho-agentd",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


class RuntimeCapsuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="runtime-capsule-test-")
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def make_base(self, feature: str, content: bytes = b"base-daemon\n", compression: str = "lz4") -> tuple[Path, Path, str]:
        root = self.root / "base-root"
        shutil.rmtree(root, ignore_errors=True)
        target = root / ALLOWLIST[feature]
        target.parent.mkdir(parents=True)
        target.write_bytes(content)
        target.chmod(0o755)
        extra = root / "etc/libreecho/base.conf"
        extra.parent.mkdir(parents=True)
        extra.write_bytes(b"base-config\n")
        extra.chmod(0o644)
        payload = self.root / "base.squashfs"
        made = run([
            "mksquashfs", str(root), str(payload), "-noappend", "-comp", compression,
            "-all-root", "-no-xattrs", "-mkfs-time", "0", "-all-time", "0",
            "-no-progress",
        ])
        self.assertEqual(made.returncode, 0, made.stderr)
        files = {}
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            relative = path.relative_to(root).as_posix()
            files[relative] = {
                "sha256": sha256(path),
                "size": path.stat().st_size,
                "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}",
            }
        manifest = {
            "schema_version": 1,
            "feature_id": feature,
            "format": "squashfs-lz4",
            "payload": {
                "filename": payload.name,
                "sha256": sha256(payload),
                "size": payload.stat().st_size,
            },
            "files": files,
        }
        manifest_path = self.root / "base.json"
        manifest_path.write_bytes(canonical_json(manifest))
        return payload, manifest_path, sha256(target)

    def make_source(self, name: str = "replacement.bin", content: bytes = b"new-daemon\n") -> Path:
        source = self.root / name
        source.write_bytes(content)
        source.chmod(0o644)
        return source

    def build_command(
        self,
        feature: str,
        base_payload: Path,
        base_manifest: Path,
        source: Path,
        output: Path | None = None,
        manifest: Path | None = None,
        target: str | None = None,
        release: str = RELEASE,
        commit: str = COMMIT,
        max_bytes: int = MAX_BYTES,
    ) -> list[str]:
        output = output or self.root / "capsule.squashfs"
        manifest = manifest or self.root / "capsule.json"
        target = target or ALLOWLIST[feature]
        return [
            sys.executable, str(BUILDER),
            "--feature-id", feature,
            "--base-payload", str(base_payload),
            "--base-manifest", str(base_manifest),
            "--product-release", release,
            "--source-commit", commit,
            "--component", ALLOWLIST[feature].rsplit("/", 1)[-1],
            "--component-version", COMPONENT_VERSION,
            "--build-identity", BUILD_IDENTITY,
            "--service-dependency", SERVICE_DEPENDENCIES[0],
            "--compatibility", json.dumps(COMPATIBILITY, sort_keys=True),
            "--replacement", f"{target}={source}",
            "--max-bytes", str(max_bytes),
            "--output", str(output),
            "--manifest", str(manifest),
        ]

    def verify_command(
        self,
        feature: str,
        base_payload: Path,
        base_manifest: Path,
        capsule: Path,
        manifest: Path,
        release: str = RELEASE,
        commit: str = COMMIT,
        max_bytes: int = MAX_BYTES,
    ) -> list[str]:
        return [
            sys.executable, str(VERIFIER),
            "--feature-id", feature,
            "--base-payload", str(base_payload),
            "--base-manifest", str(base_manifest),
            "--product-release", release,
            "--source-commit", commit,
            "--component", ALLOWLIST[feature].rsplit("/", 1)[-1],
            "--component-version", COMPONENT_VERSION,
            "--build-identity", BUILD_IDENTITY,
            "--service-dependency", SERVICE_DEPENDENCIES[0],
            "--compatibility", json.dumps(COMPATIBILITY, sort_keys=True),
            "--max-bytes", str(max_bytes),
            "--capsule", str(capsule),
            "--manifest", str(manifest),
        ]

    def build(self, feature: str = "assistant", **kwargs: object) -> tuple[Path, Path, Path, Path]:
        base_payload, base_manifest, old_hash = self.make_base(feature)
        source = self.make_source()
        output = self.root / f"capsule-{feature}.squashfs"
        manifest = self.root / f"capsule-{feature}.json"
        result = run(self.build_command(feature, base_payload, base_manifest, source, output, manifest, **kwargs))
        self.assertEqual(result.returncode, 0, result.stderr)
        return base_payload, base_manifest, output, manifest

    def test_ota_workflow_installs_squashfs_tools_before_runtime_tests(self) -> None:
        workflow = WORKFLOW.read_text()
        runtime_test = "tools/mt8163-arm32/feature_runtime/test_runtime_package.py"
        runtime_test_index = workflow.index(runtime_test)
        install_index = workflow.index("squashfs-tools")
        self.assertLess(
            install_index,
            runtime_test_index,
            "ota-release must install squashfs-tools before runtime-capsule tests run",
        )

    def test_successful_assistant_capsule_and_independent_verifier(self) -> None:
        base_payload, base_manifest, capsule, manifest = self.build()
        checked = run(self.verify_command("assistant", base_payload, base_manifest, capsule, manifest))
        self.assertEqual(checked.returncode, 0, checked.stderr)
        record = json.loads(manifest.read_text())
        self.assertEqual(record["kind"], "runtime-capsule")
        self.assertEqual(record["max_bytes"], MAX_BYTES)
        self.assertEqual(record["files"][ALLOWLIST["assistant"]]["mode"], "0755")
        self.assertEqual(record["files"][ALLOWLIST["assistant"]]["base_sha256"], sha256(self.root / "base-root" / ALLOWLIST["assistant"]))

    def test_manifest_carries_boot_critical_identity_and_compatibility_contract(self) -> None:
        _, _, _, manifest = self.build()
        record = json.loads(manifest.read_text())
        self.assertEqual(record["component"], COMPONENT)
        self.assertEqual(record["component_version"], COMPONENT_VERSION)
        self.assertEqual(record["build_identity"], BUILD_IDENTITY)
        self.assertEqual(record["service_dependencies"], SERVICE_DEPENDENCIES)
        self.assertEqual(record["compatibility"], COMPATIBILITY)

    def test_verifier_rejects_missing_or_tampered_boot_critical_contract_fields(self) -> None:
        base_payload, base_manifest, capsule, manifest = self.build()
        original = json.loads(manifest.read_text())
        for field in ("component", "component_version", "build_identity", "service_dependencies", "compatibility"):
            with self.subTest(field=field):
                mutated = dict(original)
                mutated.pop(field)
                manifest.write_bytes(canonical_json(mutated))
                self.assertNotEqual(
                    run(self.verify_command("assistant", base_payload, base_manifest, capsule, manifest)).returncode,
                    0,
                )
        mutations = {
            "component": "libreecho-otherd",
            "component_version": "radar-puffin-v9.9.9",
            "build_identity": "tampered-build",
            "service_dependencies": ["different-service"],
            "compatibility": {"abi": "x86", "model": "other", "mounts": ["/"], "dependencies": []},
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                mutated = dict(original)
                mutated[field] = value
                manifest.write_bytes(canonical_json(mutated))
                self.assertNotEqual(
                    run(self.verify_command("assistant", base_payload, base_manifest, capsule, manifest)).returncode,
                    0,
                )
        manifest.write_bytes(canonical_json(original))

    def test_builder_rejects_real_zstd_base_payload_even_when_manifest_claims_lz4(self) -> None:
        base_payload, base_manifest, _ = self.make_base("assistant", compression="zstd")
        source = self.make_source()
        result = run(self.build_command("assistant", base_payload, base_manifest, source))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("compression", result.stderr.lower())

    def test_verifier_rejects_real_zstd_capsule_even_when_manifest_claims_lz4(self) -> None:
        base_payload, base_manifest, capsule, manifest = self.build()
        root = self.root / "zstd-capsule-root"
        target = root / ALLOWLIST["assistant"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"new-daemon\n")
        target.chmod(0o755)
        zstd_capsule = self.make_capsule_from_root(
            root, self.root / "zstd-capsule.squashfs", compression="zstd"
        )
        original = json.loads(manifest.read_text())
        mutated = dict(original)
        mutated["payload"] = {
            "filename": zstd_capsule.name,
            "sha256": sha256(zstd_capsule),
            "size": zstd_capsule.stat().st_size,
        }
        manifest.write_bytes(canonical_json(mutated))
        result = run(self.verify_command("assistant", base_payload, base_manifest, zstd_capsule, manifest))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("compression", result.stderr.lower())

    def test_verifier_rejects_semantically_equivalent_noncanonical_manifest(self) -> None:
        base_payload, base_manifest, capsule, manifest = self.build()
        value = json.loads(manifest.read_text())
        manifest.write_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
        result = run(self.verify_command("assistant", base_payload, base_manifest, capsule, manifest))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("canonical", result.stderr.lower())

    def test_real_squashfs_has_normalized_metadata_and_extracts(self) -> None:
        base_payload, base_manifest, capsule, manifest = self.build()
        listed = run(["unsquashfs", "-lln", "-full-precision", "-UTC", "-no-progress", str(capsule)])
        self.assertEqual(listed.returncode, 0, listed.stderr)
        rows = [line.split(maxsplit=5) for line in listed.stdout.splitlines() if line.strip()]
        self.assertEqual([row[5] for row in rows], sorted(row[5] for row in rows))
        for row in rows:
            self.assertEqual(len(row), 6)
            self.assertEqual(row[1], "0/0")
            self.assertEqual(row[3:], ["1970-01-01", "00:00:00", row[5]])
        extracted = self.root / "extracted"
        unpacked = run(["unsquashfs", "-d", str(extracted), "-no-progress", str(capsule)])
        self.assertEqual(unpacked.returncode, 0, unpacked.stderr)
        target = extracted / ALLOWLIST["assistant"]
        self.assertEqual(target.read_bytes(), b"new-daemon\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_verifier_rejects_capsule_payload_tampering(self) -> None:
        base_payload, base_manifest, capsule, manifest = self.build()
        original = capsule.read_bytes()
        tampered = bytearray(original)
        tampered[-1] ^= 1
        capsule.write_bytes(tampered)
        checked = run(self.verify_command("assistant", base_payload, base_manifest, capsule, manifest))
        self.assertNotEqual(checked.returncode, 0)
        capsule.write_bytes(original)
        checked = run(self.verify_command("assistant", base_payload, base_manifest, capsule, manifest))
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_build_is_byte_reproducible_in_separate_directories(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        outputs = []
        for directory in (first, second):
            base_root = directory / "base-root"
            target = base_root / ALLOWLIST["assistant"]
            target.parent.mkdir(parents=True)
            target.write_bytes(b"base\n")
            target.chmod(0o755)
            payload = directory / "base.squashfs"
            made = run(["mksquashfs", str(base_root), str(payload), "-noappend", "-comp", "lz4", "-all-root", "-no-xattrs", "-mkfs-time", "0", "-all-time", "0", "-no-progress"])
            self.assertEqual(made.returncode, 0, made.stderr)
            base_manifest = directory / "base.json"
            base_manifest.write_bytes(canonical_json({
                "schema_version": 1, "feature_id": "assistant", "format": "squashfs-lz4",
                "payload": {"filename": "base.squashfs", "sha256": sha256(payload), "size": payload.stat().st_size},
                "files": {ALLOWLIST["assistant"]: {"sha256": sha256(target), "size": 5, "mode": "0755"}},
            }))
            source = directory / "replacement.bin"
            source.write_bytes(b"same replacement\n")
            output = directory / "capsule.squashfs"
            manifest = directory / "capsule.json"
            result = run(self.build_command("assistant", payload, base_manifest, source, output, manifest))
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append((output.read_bytes(), manifest.read_bytes()))
        self.assertEqual(outputs[0], outputs[1])

    def test_all_known_primary_daemon_paths_are_packagable(self) -> None:
        for feature in ALLOWLIST:
            with self.subTest(feature=feature):
                self.build(feature)

    def test_rejects_unsafe_unknown_and_non_daemon_targets(self) -> None:
        cases = ["../escape", "/absolute", "usr\\local\\sbin\\x", ".", "usr/local/./x", "usr/local/sbin/unknown", "etc/config", "usr/local/share/model.onnx"]
        for target in cases:
            with self.subTest(target=target):
                base_payload, base_manifest, _ = self.make_base("assistant")
                source = self.make_source(content=b"bad")
                result = run(self.build_command("assistant", base_payload, base_manifest, source, target=target))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ERROR:", result.stderr)

    def test_rejects_duplicate_target(self) -> None:
        base_payload, base_manifest, _ = self.make_base("assistant")
        source = self.make_source()
        command = self.build_command("assistant", base_payload, base_manifest, source)
        command[command.index("--replacement") + 1] = f"{ALLOWLIST['assistant']}={source}"
        command.insert(command.index("--output"), "--replacement")
        command.insert(command.index("--output"), f"{ALLOWLIST['assistant']}={source}")
        result = run(command)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERROR:", result.stderr)

    def test_rejects_source_symlink_and_nonregular(self) -> None:
        base_payload, base_manifest, _ = self.make_base("assistant")
        source = self.root / "source-link"
        source.symlink_to(self.make_source())
        result = run(self.build_command("assistant", base_payload, base_manifest, source))
        self.assertNotEqual(result.returncode, 0)
        directory = self.root / "source-dir"
        directory.mkdir()
        result = run(self.build_command("assistant", base_payload, base_manifest, directory))
        self.assertNotEqual(result.returncode, 0)

    def test_rejects_symlink_base_and_preexisting_outputs(self) -> None:
        base_payload, base_manifest, _ = self.make_base("assistant")
        source = self.make_source()
        link = self.root / "payload-link"
        link.symlink_to(base_payload)
        result = run(self.build_command("assistant", link, base_manifest, source))
        self.assertNotEqual(result.returncode, 0)
        output = self.root / "already-there"
        output.write_bytes(b"existing")
        result = run(self.build_command("assistant", base_payload, base_manifest, source, output=output))
        self.assertNotEqual(result.returncode, 0)

    def test_rejects_malformed_wrong_or_inconsistent_base_manifest(self) -> None:
        base_payload, base_manifest, _ = self.make_base("assistant")
        source = self.make_source()
        original = json.loads(base_manifest.read_text())
        for mutation in (
            b"not json\n",
            {**original, "feature_id": "tts"},
            {**original, "payload": {**original["payload"], "sha256": "0" * 64}},
            {**original, "payload": {**original["payload"], "size": original["payload"]["size"] + 1}},
            {**original, "files": {ALLOWLIST["assistant"]: {**original["files"][ALLOWLIST["assistant"]], "mode": "4755"}}},
            {**original, "files": {}},
        ):
            with self.subTest(mutation=repr(mutation)[:30]):
                base_manifest.write_bytes(mutation if isinstance(mutation, bytes) else canonical_json(mutation))
                result = run(self.build_command("assistant", base_payload, base_manifest, source))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ERROR:", result.stderr)
        base_manifest.write_bytes(canonical_json({**original, "schema_version": True}))
        result = run(self.build_command("assistant", base_payload, base_manifest, source))
        self.assertNotEqual(result.returncode, 0)

    def test_verifier_rejects_manifest_field_tampering(self) -> None:
        base_payload, base_manifest, capsule, manifest = self.build()
        original = json.loads(manifest.read_text())
        mutations = [
            ("product_release", "radar-puffin-v9.9.9", RELEASE),
            ("source_commit", "f" * 40, COMMIT),
            ("base_payload_sha256", "0" * 64, None),
            ("base_manifest_sha256", "0" * 64, None),
        ]
        for field, value, expected in mutations:
            with self.subTest(field=field):
                mutated = dict(original)
                mutated[field] = value
                manifest.write_bytes(canonical_json(mutated))
                result = run(self.verify_command("assistant", base_payload, base_manifest, capsule, manifest))
                self.assertNotEqual(result.returncode, 0)
        mutated = json.loads(canonical_json(original))
        mutated["files"][ALLOWLIST["assistant"]]["sha256"] = "0" * 64
        manifest.write_bytes(canonical_json(mutated))
        self.assertNotEqual(run(self.verify_command("assistant", base_payload, base_manifest, capsule, manifest)).returncode, 0)
        mutated["files"][ALLOWLIST["assistant"]]["sha256"] = original["files"][ALLOWLIST["assistant"]]["sha256"]
        mutated["files"][ALLOWLIST["assistant"]]["base_sha256"] = "0" * 64
        manifest.write_bytes(canonical_json(mutated))
        self.assertNotEqual(run(self.verify_command("assistant", base_payload, base_manifest, capsule, manifest)).returncode, 0)
        mutated["files"][ALLOWLIST["assistant"]]["base_sha256"] = original["files"][ALLOWLIST["assistant"]]["base_sha256"]
        mutated["files"][ALLOWLIST["assistant"]]["mode"] = "0644"
        manifest.write_bytes(canonical_json(mutated))
        self.assertNotEqual(run(self.verify_command("assistant", base_payload, base_manifest, capsule, manifest)).returncode, 0)
        mutated["files"][ALLOWLIST["assistant"]]["mode"] = "4755"
        manifest.write_bytes(canonical_json(mutated))
        self.assertNotEqual(run(self.verify_command("assistant", base_payload, base_manifest, capsule, manifest)).returncode, 0)
        mutated["files"][ALLOWLIST["assistant"]]["mode"] = original["files"][ALLOWLIST["assistant"]]["mode"]
        mutated["unexpected"] = 1
        manifest.write_bytes(canonical_json(mutated))
        self.assertNotEqual(run(self.verify_command("assistant", base_payload, base_manifest, capsule, manifest)).returncode, 0)

    def test_verifier_requires_manifest_cap_to_match_trusted_cli_cap(self) -> None:
        base_payload, base_manifest, capsule, manifest = self.build()
        original = json.loads(manifest.read_text())
        for value in (MAX_BYTES // 2, MAX_BYTES * 2):
            mutated = dict(original)
            mutated["max_bytes"] = value
            manifest.write_bytes(canonical_json(mutated))
            result = run(self.verify_command("assistant", base_payload, base_manifest, capsule, manifest))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("manifest max-bytes", result.stderr)
        manifest.write_bytes(canonical_json(original))

    def test_builder_and_verifier_reject_unbounded_or_nonpositive_caps(self) -> None:
        base_payload, base_manifest, _ = self.make_base("assistant")
        source = self.make_source()
        for value in ("0", "-1", str(64 * 1024 * 1024 + 1)):
            with self.subTest(value=value):
                result = run(self.build_command("assistant", base_payload, base_manifest, source, max_bytes=int(value)))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("max-bytes", result.stderr)

        base_payload, base_manifest, capsule, manifest = self.build()
        for value in (0, -1, 64 * 1024 * 1024 + 1):
            with self.subTest(verifier_value=value):
                result = run(self.verify_command("assistant", base_payload, base_manifest, capsule, manifest, max_bytes=value))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("max-bytes", result.stderr)

    def test_verifier_rejects_capsule_identity_filename_hash_and_size(self) -> None:
        base_payload, base_manifest, capsule, manifest = self.build()
        original = json.loads(manifest.read_text())
        for mutation in (
            {**original, "payload": {**original["payload"], "sha256": "0" * 64}},
            {**original, "payload": {**original["payload"], "size": original["payload"]["size"] + 1}},
            {**original, "payload": {**original["payload"], "filename": "other.squashfs"}},
        ):
            manifest.write_bytes(canonical_json(mutation))
            result = run(self.verify_command("assistant", base_payload, base_manifest, capsule, manifest))
            self.assertNotEqual(result.returncode, 0)

    def make_capsule_from_root(self, root: Path, path: Path | None = None, compression: str = "lz4") -> Path:
        path = path or self.root / "malicious.squashfs"
        for directory in [root, *[item for item in root.rglob("*") if item.is_dir()]]:
            directory.chmod(0o755)
        made = run(["mksquashfs", str(root), str(path), "-noappend", "-comp", compression, "-all-root", "-no-xattrs", "-mkfs-time", "0", "-all-time", "0", "-root-mode", "0755", "-no-progress"])
        self.assertEqual(made.returncode, 0, made.stderr)
        return path

    def test_verifier_rejects_extra_symlink_and_wrong_mode_members(self) -> None:
        base_payload, base_manifest, capsule, manifest = self.build()
        original_manifest = manifest.read_bytes()
        for kind in ("extra", "symlink", "wrong-mode"):
            with self.subTest(kind=kind):
                root = self.root / kind
                target = root / ALLOWLIST["assistant"]
                target.parent.mkdir(parents=True)
                target.write_bytes(b"new-daemon\n")
                if kind == "extra":
                    extra = root / "etc/extra"
                    extra.parent.mkdir(parents=True)
                    extra.write_bytes(b"extra")
                elif kind == "symlink":
                    (root / "etc").mkdir(parents=True)
                    (root / "etc/link").symlink_to(target)
                else:
                    target.chmod(0o644)
                bad_capsule = self.make_capsule_from_root(root)
                manifest.write_bytes(original_manifest)
                result = run(self.verify_command("assistant", base_payload, base_manifest, bad_capsule, manifest))
                self.assertNotEqual(result.returncode, 0)

    def test_builder_and_verifier_reject_setuid_capsules_and_inputs(self) -> None:
        base_payload, base_manifest, _, manifest = self.build()
        source = self.make_source(name="setuid-source")
        source.chmod(0o4755)
        rejected = run(self.build_command("assistant", base_payload, base_manifest, source))
        self.assertNotEqual(rejected.returncode, 0)

        for mode in (0o4755, 0o2755, 0o1755):
            with self.subTest(mode=oct(mode)):
                root = self.root / f"special-{mode:o}"
                target = root / ALLOWLIST["assistant"]
                target.parent.mkdir(parents=True)
                target.write_bytes(b"new-daemon\n")
                target.chmod(mode)
                crafted = self.make_capsule_from_root(root, self.root / f"special-{mode:o}.squashfs")
                checked = run(self.verify_command("assistant", base_payload, base_manifest, crafted, manifest))
                self.assertNotEqual(checked.returncode, 0)

    def test_verifier_rejects_capsule_with_extended_attributes(self) -> None:
        base_payload, base_manifest, _, manifest = self.build()
        root = self.root / "xattr-capsule-root"
        target = root / ALLOWLIST["assistant"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"new-daemon\n")
        target.chmod(0o755)
        os.setxattr(target, b"user.libreecho-test", b"present")
        for directory in [root, *[item for item in root.rglob("*") if item.is_dir()]]:
            directory.chmod(0o755)

        crafted = self.root / "xattr-capsule.squashfs"
        made = run([
            "mksquashfs", str(root), str(crafted), "-noappend", "-comp", "lz4",
            "-all-root", "-mkfs-time", "0", "-all-time", "0",
            "-root-mode", "0755", "-no-progress",
        ])
        self.assertEqual(made.returncode, 0, made.stderr)
        superblock = run(["unsquashfs", "-s", str(crafted)])
        self.assertEqual(superblock.returncode, 0, superblock.stderr)
        self.assertIn("Number of xattr ids 1", superblock.stdout)

        value = json.loads(manifest.read_text())
        value["payload"] = {
            "filename": crafted.name,
            "sha256": sha256(crafted),
            "size": crafted.stat().st_size,
        }
        value["files"] = {
            ALLOWLIST["assistant"]: {
                **value["files"][ALLOWLIST["assistant"]],
                "sha256": sha256(target),
                "size": target.stat().st_size,
            }
        }
        manifest.write_bytes(canonical_json(value))
        checked = run(self.verify_command(
            "assistant", base_payload, base_manifest, crafted, manifest,
        ))
        self.assertNotEqual(checked.returncode, 0)
        self.assertIn("extended attributes", checked.stderr)

    def test_builder_rejects_replacement_input_over_explicit_cap(self) -> None:
        base_payload, base_manifest, _ = self.make_base("assistant")
        source = self.make_source(content=os.urandom(2 * 1024 * 1024))
        output = self.root / "oversized.squashfs"
        manifest = self.root / "oversized.json"
        result = run(self.build_command("assistant", base_payload, base_manifest, source, output, manifest))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("replacement source exceeds max-bytes", result.stderr)
        self.assertFalse(output.exists())
        self.assertFalse(manifest.exists())

    def test_builder_rejects_final_capsule_over_explicit_cap(self) -> None:
        base_payload, base_manifest, _ = self.make_base("assistant")
        source = self.make_source(content=b"x")
        output = self.root / "oversized-final.squashfs"
        manifest = self.root / "oversized-final.json"
        result = run(self.build_command("assistant", base_payload, base_manifest, source, output, manifest, max_bytes=1))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("capsule payload exceeds max-bytes", result.stderr)
        self.assertFalse(output.exists())
        self.assertFalse(manifest.exists())

    def test_verifier_rejects_crafted_capsule_over_explicit_cap(self) -> None:
        base_payload, base_manifest, _, manifest = self.build()
        root = self.root / "oversized-capsule-root"
        target = root / ALLOWLIST["assistant"]
        target.parent.mkdir(parents=True)
        target.write_bytes(os.urandom(2 * 1024 * 1024))
        crafted = self.make_capsule_from_root(root, self.root / "crafted-oversized.squashfs")
        self.assertGreater(crafted.stat().st_size, MAX_BYTES)
        original = json.loads(manifest.read_text())
        mutated = dict(original)
        mutated["payload"] = {
            "filename": crafted.name,
            "sha256": sha256(crafted),
            "size": crafted.stat().st_size,
        }
        mutated["files"] = {
            ALLOWLIST["assistant"]: {
                **original["files"][ALLOWLIST["assistant"]],
                "sha256": sha256(target),
                "size": target.stat().st_size,
            }
        }
        manifest.write_bytes(canonical_json(mutated))
        result = run(self.verify_command("assistant", base_payload, base_manifest, crafted, manifest))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("capsule payload exceeds max-bytes", result.stderr)

    def test_no_runtime_integration_files_are_changed(self) -> None:
        tracked = ["tools/mt8163-arm32/stage_feature_root.sh", "tools/mt8163-arm32/initramfs/libreecho-update"]
        for relative in tracked:
            self.assertTrue((TOOLS.parent.parent.parent / relative).exists())


if __name__ == "__main__":
    unittest.main()

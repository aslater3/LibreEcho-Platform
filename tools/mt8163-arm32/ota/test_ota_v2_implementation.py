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
BOOT_SIZE = 16 * 1024 * 1024
KEY = bytes(range(32))
FEATURE_IDS = ("airplay2", "tts", "wakeword", "stt", "assistant")


def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=False, **kwargs)


def generated_host_fixture(source: Path, root: Path, replacements: dict[str, str]) -> Path:
    """Generate a non-shipped fixture copy with immutable target paths rewritten."""
    text = source.read_text()
    for old, new in replacements.items():
        if old not in text:
            raise AssertionError(f"fixture anchor missing: {old}")
        text = text.replace(old, new)
    fixture = root / f"{source.name}.host-fixture"
    fixture.write_text(text)
    fixture.chmod(0o755)
    return fixture


def transaction_fixture(root: Path, env: dict[str, str], source: Path = TRANSACTION) -> Path:
    env = dict(env)
    env.setdefault("LIBREECHO_TRANSACTION_ROOT", str(root / "data/libreecho/update"))
    env.setdefault("LIBREECHO_FEATURE_ROOT", str(root / "data/libreecho/features"))
    env.setdefault("LIBREECHO_BCB_FILE", str(root / "bcb"))
    env.setdefault("LIBREECHO_BOOT_PARTITION_DIR", str(root / "partitions"))
    env.setdefault("LIBREECHO_VERIFY_BIN", str(root / "verify-ed25519.py"))
    env.setdefault("LIBREECHO_PUBLIC_KEY", str(root / "public-key.hex"))
    values = {
        "BB=/bin/busybox": f"BB={shlex.quote(env.get('BB', '/bin/busybox'))}",
        "ROOT=/data/libreecho/update": f"ROOT={shlex.quote(env['LIBREECHO_TRANSACTION_ROOT'])}",
        "FEATURES=/data/libreecho/features": f"FEATURES={shlex.quote(env['LIBREECHO_FEATURE_ROOT'])}",
        "BCB_FILE=$ROOT/staging/bootctl.readback": f"BCB_FILE={shlex.quote(env['LIBREECHO_BCB_FILE'])}",
        "SPACE_RESERVE_BYTES=262144": f"SPACE_RESERVE_BYTES={env.get('LIBREECHO_TRANSACTION_RESERVE_BYTES', '262144')}",
        "SPACE_METADATA_BYTES=32768": f"SPACE_METADATA_BYTES={env.get('LIBREECHO_TRANSACTION_METADATA_BYTES', '32768')}",
        "SPACE_RETAINED_EVIDENCE_BYTES=65536": f"SPACE_RETAINED_EVIDENCE_BYTES={env.get('LIBREECHO_TRANSACTION_RETAINED_EVIDENCE_BYTES', '65536')}",
        "SPACE_JOURNAL_BYTES=32768": f"SPACE_JOURNAL_BYTES={env.get('LIBREECHO_TRANSACTION_JOURNAL_BYTES', '32768')}",
        "SPACE_RENAME_BYTES=16384": f"SPACE_RENAME_BYTES={env.get('LIBREECHO_TRANSACTION_RENAME_BYTES', '16384')}",
        "SPACE_PHASE=prewrite": f"SPACE_PHASE={env.get('LIBREECHO_TRANSACTION_PHASE', 'prewrite')}",
        "SPACE_DF=df": f"SPACE_DF={shlex.quote(env.get('LIBREECHO_TRANSACTION_DF_BIN', 'df'))}",
        "SPACE_STAT=stat": f"SPACE_STAT={shlex.quote(env.get('LIBREECHO_TRANSACTION_STAT_BIN', 'stat'))}",
        "PROC_ROOT=/proc": f"PROC_ROOT={shlex.quote(env.get('LIBREECHO_PROC_ROOT', '/proc'))}",
        "VAR_RUN_ROOT=/var/run": f"VAR_RUN_ROOT={shlex.quote(env.get('LIBREECHO_VAR_RUN_ROOT', '/var/run'))}",
        "ETC_ROOT=/etc": f"ETC_ROOT={shlex.quote(env.get('LIBREECHO_ETC_ROOT', '/etc'))}",
        "RUN_ROOT=/run": f"RUN_ROOT={shlex.quote(env.get('LIBREECHO_RUN_ROOT', '/run'))}",
        "CONFIG_FILE=/data/libreecho/config/web-config.json": f"CONFIG_FILE={shlex.quote(env.get('LIBREECHO_FEATURE_CONFIG', str(root / 'config.json')))}",
        "VERIFY=/usr/local/libexec/libreecho-update-verify": f"VERIFY={shlex.quote(env['LIBREECHO_VERIFY_BIN'])}",
        "PUBLIC_KEY=/etc/libreecho/ota-public-key.hex": f"PUBLIC_KEY={shlex.quote(env['LIBREECHO_PUBLIC_KEY'])}",
        "TRANSACTION_SLOT_FILE=$ROOT/transaction-slot": f"TRANSACTION_SLOT_FILE={shlex.quote(env['LIBREECHO_TRANSACTION_ROOT'])}/transaction-slot",
        "BOOT_PARTITION=/dev/mmcblk0p10": f"BOOT_PARTITION={shlex.quote(env['LIBREECHO_BOOT_PARTITION_DIR'])}/boot_a",
        "BOOT_PARTITION=/dev/mmcblk0p11": f"BOOT_PARTITION={shlex.quote(env['LIBREECHO_BOOT_PARTITION_DIR'])}/boot_b",
    }
    slot = Path(env["LIBREECHO_TRANSACTION_ROOT"]) / "transaction-slot"
    slot.parent.mkdir(parents=True, exist_ok=True)
    slot.write_text(env.get("LIBREECHO_TRANSACTION_SLOT", "b") + "\n")
    return generated_host_fixture(source, root, values)


def updater_fixture(root: Path, env: dict[str, str], transaction: Path) -> Path:
    env = dict(env)
    env.setdefault("LIBREECHO_DATA_ROOT", str(root / "data"))
    env.setdefault("LIBREECHO_UPDATE_ROOT", str(root / "data/libreecho/update"))
    env.setdefault("LIBREECHO_FEATURE_ROOT", str(root / "data/libreecho/features"))
    env.setdefault("LIBREECHO_BCB_FILE", str(root / "bcb"))
    env.setdefault("LIBREECHO_BOOT_PARTITION_DIR", str(root / "partitions"))
    env.setdefault("LIBREECHO_PACKAGED_CHANNEL_FILE", str(root / "packaged-channel"))
    bcb = Path(env["LIBREECHO_BCB_FILE"])
    bcb.parent.mkdir(parents=True, exist_ok=True)
    if not bcb.exists():
        bcb.write_text("selected_slot=a\nslot_a_success=1\n")
    data = Path(env["LIBREECHO_DATA_ROOT"])
    update = Path(env["LIBREECHO_UPDATE_ROOT"])
    helper_env = dict(env)
    helper_env["LIBREECHO_BCB_FILE"] = str(update / "staging/bootctl.readback")
    transaction = transaction_fixture(root, helper_env)
    device = root / "dev"
    sys_block = root / "sys/class/block"
    mounts = root / "proc-mounts"
    device.mkdir(parents=True, exist_ok=True)
    (sys_block / "mmcblk0p16").mkdir(parents=True, exist_ok=True)
    (sys_block / "mmcblk0p7").mkdir(parents=True, exist_ok=True)
    for slot, partname in (("a", "boot_a_x"), ("b", "boot_b_x")):
        (sys_block / f"mmcblk0p{10 if slot == 'a' else 11}").mkdir(parents=True, exist_ok=True)
        (sys_block / f"mmcblk0p{10 if slot == 'a' else 11}/uevent").write_text(f"PARTNAME={partname}\n")
        (sys_block / f"mmcblk0p{10 if slot == 'a' else 11}/size").write_text("32768\n")
    (sys_block / "mmcblk0p16/uevent").write_text("PARTNAME=userdata\n")
    (sys_block / "mmcblk0p16/size").write_text("2137088\n")
    (sys_block / "mmcblk0p7/uevent").write_text("PARTNAME=expdb\n")
    (sys_block / "mmcblk0p7/size").write_text("20480\n")
    for name in ("mmcblk0p7", "mmcblk0p16"):
        (device / name).write_bytes(b"\0" * 512)
    parts = Path(env["LIBREECHO_BOOT_PARTITION_DIR"])
    for slot, name in (("a", "mmcblk0p10"), ("b", "mmcblk0p11")):
        link = parts / name
        if not link.exists():
            link.symlink_to(parts / f"boot_{slot}")
    mounts.write_text(f"fixture {data} rw\n")
    values = {
        "BB=/bin/busybox": f"BB={shlex.quote(env.get('BB', '/bin/busybox'))}",
        "DATA_ROOT=/data": f"DATA_ROOT={shlex.quote(str(data))}",
        "FEATURE_ROOT=$DATA_ROOT/libreecho/features": "FEATURE_ROOT=$DATA_ROOT/libreecho/features",
        "UPDATE_ROOT=$DATA_ROOT/libreecho/update": "UPDATE_ROOT=$DATA_ROOT/libreecho/update",
        "VERIFY=/usr/local/libexec/libreecho-update-verify": f"VERIFY={shlex.quote(env['LIBREECHO_VERIFY_BIN'])}",
        "BOOTCTL=/usr/local/sbin/libreecho-bootctl": f"BOOTCTL={shlex.quote(env['LIBREECHO_BOOTCTL'])}",
        "PUBLIC_KEY=/etc/libreecho/ota-public-key.hex": f"PUBLIC_KEY={shlex.quote(env['LIBREECHO_PUBLIC_KEY'])}",
        "PACKAGED_CHANNEL_FILE=/etc/libreecho/update-channel": f"PACKAGED_CHANNEL_FILE={shlex.quote(env['LIBREECHO_PACKAGED_CHANNEL_FILE'])}",
        "FEATURE_TRANSACTION=/usr/local/sbin/libreecho-feature-transaction": f"FEATURE_TRANSACTION={shlex.quote(str(transaction))}",
        "USERDATA_DEVICE=/dev/mmcblk0p16": f"USERDATA_DEVICE={shlex.quote(str(device / 'mmcblk0p16'))}",
        "USERDATA_SYS=/sys/class/block/mmcblk0p16": f"USERDATA_SYS={shlex.quote(str(sys_block / 'mmcblk0p16'))}",
        "MOUNTS_FILE=/proc/mounts": f"MOUNTS_FILE={shlex.quote(str(mounts))}",
        "EXPDB_DEVICE=/dev/mmcblk0p7": f"EXPDB_DEVICE={shlex.quote(str(device / 'mmcblk0p7'))}",
        "EXPDB_SYS=/sys/class/block/mmcblk0p7": f"EXPDB_SYS={shlex.quote(str(sys_block / 'mmcblk0p7'))}",
        "TRANSACTION_BCB_FILE=$STAGING/bootctl.readback": f"TRANSACTION_BCB_FILE={shlex.quote(str(update / 'staging/bootctl.readback'))}",
        "BOOT_DEVICE_ROOT=/dev": f"BOOT_DEVICE_ROOT={shlex.quote(str(parts))}",
        "BOOT_SYS_ROOT=/sys/class/block": f"BOOT_SYS_ROOT={shlex.quote(str(sys_block))}",
        "[ -b ": "[ -f ",
    }
    source = TOOLS / "initramfs/libreecho-update"
    return generated_host_fixture(source, root, values)


def fetch_fixture(root: Path, env: dict[str, str], updater: Path, transaction: Path) -> Path:
    client = root / "client"
    curl = Path(env["LIBREECHO_FETCH_CURL"])
    proc_root = Path(env["LIBREECHO_PROC_ROOT"])
    proc_root.mkdir(parents=True, exist_ok=True)
    Path(env["LIBREECHO_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
    (proc_root / "mounts").write_text(f"fixture {env['LIBREECHO_DATA_ROOT']} rw\n")
    (client / "usr/local/libexec").mkdir(parents=True, exist_ok=True)
    (client / "usr/local/share/libreecho").mkdir(parents=True, exist_ok=True)
    (proc_root / "mounts").write_text(
        f"fixture {env['LIBREECHO_DATA_ROOT']} rw\nfixture {client} ro\n"
    )
    shutil.copy2(curl, client / "usr/local/libexec/libreecho-curl")
    (client / "usr/local/share/libreecho/cacert.pem").write_text("fixture-ca\n")
    assistant_manifest = root / "assistant-manifest.json"
    payload_path = Path(env["LIBREECHO_ASSISTANT_PAYLOAD"])
    assistant_manifest.write_text(json.dumps({
        "payload": {"size": payload_path.stat().st_size, "sha256": hashlib.sha256(payload_path.read_bytes()).hexdigest()}
    }, indent=2))
    values = {
        "BB=/bin/busybox": f"BB={shlex.quote(env.get('BB', '/bin/busybox'))}",
        "UPDATE=/usr/local/sbin/libreecho-update": f"UPDATE={shlex.quote(str(updater))}",
        "CONFIG=/etc/libreecho/ota-source.conf": f"CONFIG={shlex.quote(env['LIBREECHO_FETCH_CONFIG'])}",
        "PROFILE=/etc/libreecho/image-profile": f"PROFILE={shlex.quote(env['LIBREECHO_FETCH_PROFILE'])}",
        "ROOT=/data/libreecho/update": f"ROOT={shlex.quote(env['LIBREECHO_FETCH_ROOT'])}",
        "PACKAGED_CHANNEL_FILE=/etc/libreecho/update-channel": f"PACKAGED_CHANNEL_FILE={shlex.quote(env['LIBREECHO_FETCH_PACKAGED_CHANNEL'])}",
        "ASSISTANT_PAYLOAD=/data/libreecho/features/assistant/payload.squashfs": f"ASSISTANT_PAYLOAD={shlex.quote(env['LIBREECHO_ASSISTANT_PAYLOAD'])}",
        "ASSISTANT_MANIFEST=/data/libreecho/features/assistant/manifest.json": f"ASSISTANT_MANIFEST={shlex.quote(str(assistant_manifest))}",
        "CLIENT_ROOT=/run/libreecho/ota-client": f"CLIENT_ROOT={shlex.quote(str(client))}",
        "CURL_STDERR=/run/libreecho/ota-curl.stderr": f"CURL_STDERR={shlex.quote(env['LIBREECHO_FETCH_CURL_STDERR'])}",
        "CURL_HEADERS=/run/libreecho/ota-curl.headers": f"CURL_HEADERS={shlex.quote(env.get('LIBREECHO_FETCH_CURL_HEADERS', str(root / 'curl.headers')))}",
        "FEATURE_TRANSACTION=/usr/local/sbin/libreecho-feature-transaction": f"FEATURE_TRANSACTION={shlex.quote(str(transaction))}",
        "DATA_ROOT=/data": f"DATA_ROOT={shlex.quote(env['LIBREECHO_DATA_ROOT'])}",
        "PROC_ROOT=/proc": f"PROC_ROOT={shlex.quote(env['LIBREECHO_PROC_ROOT'])}",
        "RUN_ROOT=/run": f"RUN_ROOT={shlex.quote(str(root / 'run'))}",
        '[ "$($BB stat -c %u "$ASSISTANT_PAYLOAD" 2>/dev/null)" = 0 ]': f'[ "$($BB stat -c %u "$ASSISTANT_PAYLOAD" 2>/dev/null)" = {os.getuid()} ]',
        '[ "$($BB stat -c %u "$ASSISTANT_MANIFEST" 2>/dev/null)" = 0 ]': f'[ "$($BB stat -c %u "$ASSISTANT_MANIFEST" 2>/dev/null)" = {os.getuid()} ]',
    }
    source = TOOLS / "initramfs/libreecho-update-fetch"
    return generated_host_fixture(source, root, values)


def write_real_verifier(root: Path, public_key: Path, signing_key: SigningKey) -> Path:
    """Create a cryptographic verifier fixture; never bypass the shell gate."""
    verifier = root / "verify-ed25519.py"
    verifier.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from nacl.signing import VerifyKey\n"
        "try:\n"
        "    key = bytes.fromhex(Path(sys.argv[1]).read_text().strip())\n"
        "    signature = bytes.fromhex(Path(sys.argv[3]).read_text().strip())\n"
        "    VerifyKey(key).verify(Path(sys.argv[2]).read_bytes(), signature)\n"
        "except Exception:\n"
        "    raise SystemExit(1)\n"
        "print('ota_manifest_signature=PASS')\n"
    )
    verifier.chmod(0o755)
    public_key.write_text(signing_key.verify_key.encode().hex() + "\n")
    return verifier


def write_control_tar(path: Path, manifest_bytes: bytes, signing_key: SigningKey) -> None:
    signature = signing_key.sign(manifest_bytes).signature.hex().encode("ascii") + b"\n"
    boot = b"ANDROID!" + bytes(BOOT_SIZE - 8)
    with tarfile.open(path, "w:") as archive:
        for name, data in (("manifest", manifest_bytes), ("manifest.sig", signature), ("boot.img", boot)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))


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


class AuthenticationBoundaryTests(unittest.TestCase):
    def test_allow_unsigned_v2_rejected_before_userdata_staging_or_bootctl(self) -> None:
        from feature_manifest import serialize_manifest
        with tempfile.TemporaryDirectory(prefix="ota-v2-auth-boundary-") as directory:
            root = Path(directory)
            package = root / "update.ota.tar"
            signing_key = SigningKey.generate()
            obj = manifest([feature("assistant", "preserve")])
            boot = b"ANDROID!" + bytes(BOOT_SIZE - 8)
            obj["boot_sha256"] = hashlib.sha256(boot).hexdigest()
            raw = serialize_manifest(obj)
            write_control_tar(package, raw, signing_key)
            public = root / "ota-public-key.hex"
            verifier = write_real_verifier(root, public, signing_key)
            data = root / "data"
            update = data / "libreecho/update"
            boot_partition_dir = root / "partitions"
            boot_partition_dir.mkdir()
            boot_a = boot_partition_dir / "boot_a"
            boot_a.write_bytes(bytes(BOOT_SIZE))
            boot_before = boot_a.read_bytes()
            bootctl_log = root / "bootctl.log"
            bootctl = root / "bootctl"
            bootctl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$BOOTCTL_LOG\"\nexit 0\n")
            bootctl.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "LIBREECHO_TEST_MODE": "1",
                "LIBREECHO_DATA_ROOT": str(data),
                "LIBREECHO_UPDATE_ROOT": str(update),
                "LIBREECHO_FEATURE_ROOT": str(data / "libreecho/features"),
                "LIBREECHO_BOOT_PARTITION_DIR": str(boot_partition_dir),
                "LIBREECHO_BOOTCTL": str(bootctl),
                "BOOTCTL_LOG": str(bootctl_log),
                "LIBREECHO_VERIFY_BIN": str(verifier),
                "LIBREECHO_PUBLIC_KEY": str(public),
            })
            result = run([
                "/bin/busybox", "sh", str(TOOLS / "initramfs/libreecho-update"),
                "install", "--allow-unsigned", str(package),
            ], env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsigned_v2_rejected", result.stderr)
            self.assertFalse(update.exists())
            self.assertFalse(bootctl_log.exists())
            self.assertEqual(boot_a.read_bytes(), boot_before)

    def test_production_entrypoint_ignores_hostile_env_root_key_bootctl_and_partition(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ota-v2-hostile-env-") as directory:
            root = Path(directory)
            package = root / "v1.ota.tar"
            with tarfile.open(package, "w") as archive:
                for name, payload in (("manifest", b"format=libreecho-ota-v1\n"), ("manifest.sig", b"unsigned\n"), ("boot.img", b"hostile")):
                    info = tarfile.TarInfo(name); info.size = len(payload); archive.addfile(info, io.BytesIO(payload))
            injected_log = root / "injected.log"
            injected = root / "injected"
            injected.write_text(f"#!/bin/sh\nprintf injected >> {injected_log}\nexit 0\n")
            injected.chmod(0o755)
            hostile = os.environ.copy()
            hostile.update({
                "LIBREECHO_TEST_MODE": "1",
                "LIBREECHO_DATA_ROOT": str(root / "data"),
                "LIBREECHO_UPDATE_ROOT": str(root / "update"),
                "LIBREECHO_BOOT_PARTITION_DIR": str(root / "partitions"),
                "LIBREECHO_VERIFY_BIN": str(injected),
                "LIBREECHO_BOOTCTL": str(injected),
                "LIBREECHO_PUBLIC_KEY": str(root / "attacker-key.hex"),
            })
            result = run([
                "/bin/busybox", "sh", str(TOOLS / "initramfs/libreecho-update"),
                "install", "--allow-unsigned", str(package),
            ], env=hostile)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("userdata_identity_or_mount_failed", result.stderr)
            self.assertFalse(injected_log.exists())
            self.assertFalse((root / "data").exists())

    def test_signed_malformed_v2_fixtures_fail_before_boot_write(self) -> None:
        from feature_manifest import serialize_manifest
        mutations = {
            "partial_feature_set": lambda raw: b"".join(
                (b"feature_ids=assistant\n" if line.startswith(b"feature_ids=") else line)
                for line in raw.splitlines(keepends=True)
                if not line.startswith((b"feature_airplay2_", b"feature_tts_", b"feature_wakeword_", b"feature_stt_"))
            ),
            "unknown": lambda raw: raw + b"unknown_field=1\n",
            "missing": lambda raw: raw.replace(b"manifest_version=1\n", b""),
            "unsafe_asset": lambda raw: raw.replace(
                b"libreecho-radar-puffin-0.13.11-assistant.payload.squashfs",
                b"../assistant.payload.squashfs",
            ),
        }
        with tempfile.TemporaryDirectory(prefix="ota-v2-malformed-shell-") as directory:
            root = Path(directory)
            signing_key = SigningKey.generate()
            public = root / "ota-public-key.hex"
            verifier = write_real_verifier(root, public, signing_key)
            data = root / "data"
            (data / "libreecho").mkdir(parents=True)
            (data / "libreecho/automatic-updates").write_text("channel=stable\n")
            packaged_channel = root / "packaged-channel"
            packaged_channel.write_text("stable\n")
            parts = root / "parts"
            parts.mkdir()
            boot_a = parts / "boot_a"
            boot_a.write_bytes(bytes(BOOT_SIZE))
            boot_before = boot_a.read_bytes()
            bcb = root / "bcb"
            bcb.write_text("selected_slot=a\nslot_a_success=1\n")
            bootctl_log = root / "bootctl.log"
            bootctl = root / "bootctl"
            bootctl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$BOOTCTL_LOG\"\nexit 0\n")
            bootctl.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "LIBREECHO_TEST_MODE": "1",
                "LIBREECHO_DATA_ROOT": str(data),
                "LIBREECHO_UPDATE_ROOT": str(data / "libreecho/update"),
                "LIBREECHO_FEATURE_ROOT": str(data / "libreecho/features"),
                "LIBREECHO_BOOT_PARTITION_DIR": str(parts),
                "LIBREECHO_BCB_FILE": str(bcb),
                "LIBREECHO_PACKAGED_CHANNEL_FILE": str(packaged_channel),
                "LIBREECHO_BOOTCTL": str(bootctl),
                "BOOTCTL_LOG": str(bootctl_log),
                "LIBREECHO_VERIFY_BIN": str(verifier),
                "LIBREECHO_PUBLIC_KEY": str(public),
            })
            transaction = transaction_fixture(root, env)
            updater = updater_fixture(root, env, transaction)
            from feature_manifest import build_control_tar
            for name, mutate in mutations.items():
                obj = manifest([feature("assistant", "replace")])
                boot = b"ANDROID!" + bytes(BOOT_SIZE - 8)
                obj["boot_sha256"] = hashlib.sha256(boot).hexdigest()
                raw = mutate(serialize_manifest(obj))
                package = root / f"{name}.ota.tar"
                signature = signing_key.sign(raw).signature.hex().encode("ascii") + b"\n"
                with tarfile.open(package, "w") as archive:
                    for member, payload in (("manifest", raw), ("manifest.sig", signature), ("boot.img", boot)):
                        info = tarfile.TarInfo(member); info.size = len(payload); info.mode = 0o644
                        archive.addfile(info, io.BytesIO(payload))
                result = run([
                    "/bin/busybox", "sh", str(updater), "install", str(package),
                ], env=env)
                self.assertNotEqual(result.returncode, 0, name)
                self.assertIn("ERROR:v2_", result.stderr, name)
                self.assertFalse(bootctl_log.exists(), name)
                self.assertEqual(boot_a.read_bytes(), boot_before, name)
                self.assertEqual(bcb.read_text(), "selected_slot=a\nslot_a_success=1\n", name)
                update = data / "libreecho/update"
                for child in update.iterdir():
                    if child.is_dir():
                        import shutil
                        shutil.rmtree(child)
                    else:
                        child.unlink()


class TransactionFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ota-v2-transaction-")
        self.root = Path(self.tmp.name)
        for feature_id in FEATURE_IDS:
            (self.root / f"staging/features/{feature_id}").mkdir(parents=True)
            (self.root / f"features/{feature_id}").mkdir(parents=True)
        (self.root / "features/assistant/payload.squashfs").write_bytes(b"old")
        (self.root / "features/assistant/manifest.json").write_bytes(b"old-manifest")
        (self.root / "features/assistant/runtime.squashfs").write_bytes(b"incompatible-old-runtime")
        (self.root / "features/assistant/runtime-manifest.json").write_bytes(b"incompatible-old-runtime-manifest")
        records = []
        for feature_id in FEATURE_IDS:
            if feature_id == "assistant":
                record = feature(feature_id, "replace")
                record.update({
                    "base_payload_sha256": hashlib.sha256(b"old").hexdigest(),
                    "base_manifest_sha256": hashlib.sha256(b"old-manifest").hexdigest(),
                    "asset": "libreecho-radar-puffin-0.13.11-assistant.payload.squashfs",
                    "size": 3,
                    "sha256": hashlib.sha256(b"new").hexdigest(),
                    "manifest_asset": "libreecho-radar-puffin-0.13.11-assistant.manifest.json",
                    "manifest_size": 8,
                    "manifest_sha256": hashlib.sha256(b"manifest").hexdigest(),
                })
            else:
                payload = f"{feature_id}-base-payload".encode()
                metadata = f"{feature_id}-base-manifest".encode()
                (self.root / f"features/{feature_id}/payload.squashfs").write_bytes(payload)
                (self.root / f"features/{feature_id}/manifest.json").write_bytes(metadata)
                record = feature(feature_id, "preserve")
                record.update({
                    "base_payload_sha256": hashlib.sha256(payload).hexdigest(),
                    "base_manifest_sha256": hashlib.sha256(metadata).hexdigest(),
                })
            records.append(record)
        (self.root / "staging/features/assistant/libreecho-radar-puffin-0.13.11-assistant.payload.squashfs").write_bytes(b"new")
        (self.root / "staging/features/assistant/libreecho-radar-puffin-0.13.11-assistant.manifest.json").write_bytes(b"manifest")
        from feature_manifest import serialize_manifest
        (self.root / "staging/manifest").write_bytes(serialize_manifest(manifest(records)))
        self.signing_key = SigningKey.generate()
        self.public_key = self.root / "public-key.hex"
        self.verify = write_real_verifier(self.root, self.public_key, self.signing_key)
        (self.root / "staging/manifest.sig").write_text(
            self.signing_key.sign((self.root / "staging/manifest").read_bytes()).signature.hex() + "\n"
        )
        self.bcb = self.root / "bcb"
        self.bcb.write_text("selected_slot=b\nslot_b_success=1\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def invoke(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({"LIBREECHO_TRANSACTION_ROOT": str(self.root), "LIBREECHO_FEATURE_ROOT": str(self.root / "features"), "LIBREECHO_BCB_FILE": str(self.bcb), "LIBREECHO_TRANSACTION_TEST_MODE": "1", "LIBREECHO_TRANSACTION_SLOT": "b", "LIBREECHO_VERIFY_BIN": str(self.verify), "LIBREECHO_PUBLIC_KEY": str(self.public_key)})
        if extra_env:
            env.update(extra_env)
        fixture = transaction_fixture(self.root, env)
        return run([str(fixture), *args], env=env)

    def fake_space_bins(self, available_1k: int) -> dict[str, str]:
        df = self.root / "fake-df"
        df.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
            f"printf '%s\\n' 'fixture 999999 0 {available_1k} 0% /data'\n"
        )
        df.chmod(0o755)
        stat = self.root / "fake-stat"
        stat.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  '-f -c %S '* ) printf '%s\\n' 4096 ;;\n"
            "  '-c %b %B '* ) printf '%s\\n' '1 4096' ;;\n"
            "  '-c %s '* ) /usr/bin/stat \"$@\" ;;\n"
            "  * ) exit 1 ;;\n"
            "esac\n"
        )
        stat.chmod(0o755)
        return {
            "LIBREECHO_TRANSACTION_DF_BIN": str(df),
            "LIBREECHO_TRANSACTION_STAT_BIN": str(stat),
        }

    def test_space_gate_rounds_download_files_and_rejects_one_block_short(self) -> None:
        for child in (self.root / "staging/features/assistant").iterdir():
            child.unlink()
        exact = 416
        env = self.fake_space_bins(exact) | {
            "LIBREECHO_TRANSACTION_PHASE": "download",
            "LIBREECHO_TRANSACTION_RESERVE_BYTES": "262144",
            "LIBREECHO_TRANSACTION_METADATA_BYTES": "32768",
            "LIBREECHO_TRANSACTION_RETAINED_EVIDENCE_BYTES": "65536",
            "LIBREECHO_TRANSACTION_JOURNAL_BYTES": "32768",
            "LIBREECHO_TRANSACTION_RENAME_BYTES": "16384",
        }
        enough = self.invoke("preflight", str(self.root / "staging/manifest"), extra_env=env)
        self.assertEqual(enough.returncode, 0, enough.stderr)
        self.assertIn("required_bytes=425984", enough.stdout)
        self.assertIn("available_bytes=425984", enough.stdout)
        short = self.invoke("preflight", str(self.root / "staging/manifest"), extra_env=env | {"LIBREECHO_TRANSACTION_DF_BIN": self.fake_space_bins(exact - 1)["LIBREECHO_TRANSACTION_DF_BIN"]})
        self.assertNotEqual(short.returncode, 0)
        self.assertIn("insufficient-space", short.stderr)

    def test_space_gate_rejects_malformed_and_overflow_sizes(self) -> None:
        env = self.fake_space_bins(1000) | {"LIBREECHO_TRANSACTION_PHASE": "download"}
        policy = self.invoke(
            "preflight", str(self.root / "staging/manifest"),
            extra_env=env | {"LIBREECHO_TRANSACTION_RESERVE_BYTES": "9223372036854775808"},
        )
        self.assertNotEqual(policy.returncode, 0)
        self.assertIn("space-policy", policy.stderr)
        manifest_path = self.root / "staging/manifest"
        manifest_path.write_text(
            manifest_path.read_text().replace("feature_assistant_size=3", "feature_assistant_size=9223372036854775808")
        )
        (self.root / "staging/manifest.sig").write_text(
            self.signing_key.sign(manifest_path.read_bytes()).signature.hex() + "\n"
        )
        malformed = self.invoke("preflight", str(manifest_path), extra_env=env)
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("feature-size", malformed.stderr)

    def test_space_gate_rejects_same_size_corrupt_final_before_download(self) -> None:
        final = self.root / "staging/features/assistant/libreecho-radar-puffin-0.13.11-assistant.payload.squashfs"
        final.write_bytes(b"bad")
        env = self.fake_space_bins(400) | {"LIBREECHO_TRANSACTION_PHASE": "download"}
        result = self.invoke("preflight", str(self.root / "staging/manifest"), extra_env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("insufficient-space", result.stderr)

    def test_transaction_rejects_signed_partial_feature_graph_before_prepare(self) -> None:
        manifest_path = self.root / "staging/manifest"
        full = manifest_path.read_bytes()
        partial = b"".join(
            (b"feature_ids=assistant\n" if line.startswith(b"feature_ids=") else line)
            for line in full.splitlines(keepends=True)
            if not line.startswith((b"feature_airplay2_", b"feature_tts_", b"feature_wakeword_", b"feature_stt_"))
        )
        manifest_path.write_bytes(partial)
        (self.root / "staging/manifest.sig").write_text(
            self.signing_key.sign(partial).signature.hex() + "\n"
        )
        result = self.invoke("preflight", str(manifest_path), extra_env=self.fake_space_bins(1000))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("feature-set", result.stderr)
        self.assertFalse((self.root / "pending").exists())
        self.assertFalse((self.root / "feature-commit").exists())

    def test_space_gate_prewrite_rounds_rollback_and_destination_once(self) -> None:
        exact = 416
        env = self.fake_space_bins(exact) | {
            "LIBREECHO_TRANSACTION_PHASE": "prewrite",
            "LIBREECHO_TRANSACTION_RESERVE_BYTES": "262144",
            "LIBREECHO_TRANSACTION_METADATA_BYTES": "32768",
            "LIBREECHO_TRANSACTION_RETAINED_EVIDENCE_BYTES": "65536",
            "LIBREECHO_TRANSACTION_JOURNAL_BYTES": "32768",
            "LIBREECHO_TRANSACTION_RENAME_BYTES": "16384",
        }
        enough = self.invoke("preflight", str(self.root / "staging/manifest"), extra_env=env)
        self.assertEqual(enough.returncode, 0, enough.stderr)
        self.assertIn("required_bytes=425984", enough.stdout)
        self.assertIn("available_bytes=425984", enough.stdout)
        short_stat = self.fake_space_bins(exact - 1)
        short = self.invoke("prepare-boot", extra_env=env | short_stat)
        self.assertNotEqual(short.returncode, 0)
        self.assertIn("insufficient-space", short.stderr)
        self.assertFalse((self.root / "pending").exists())
        self.assertFalse((self.root / "feature-commit").exists())

    def test_prepare_is_durable_before_commit_and_recovery_is_idempotent(self) -> None:
        prepared = self.invoke("prepare-boot")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertTrue((self.root / "pending").is_file())
        journal = (self.root / "feature-commit").read_text()
        self.assertIn("schema=2\n", journal)
        self.assertIn(f"manifest_sha256={hashlib.sha256((self.root / 'staging/manifest').read_bytes()).hexdigest()}", journal)
        self.assertIn(f"manifest_sig_sha256={hashlib.sha256((self.root / 'staging/manifest.sig').read_bytes()).hexdigest()}", journal)
        self.assertIn(f"feature_assistant_old_payload_sha256={hashlib.sha256(b'old').hexdigest()}", journal)
        self.assertIn(f"feature_assistant_new_payload_sha256={hashlib.sha256(b'new').hexdigest()}", journal)
        self.assertEqual(self.invoke("commit-after-confirm").returncode, 0)
        self.assertEqual(self.invoke("commit-after-confirm").returncode, 0)
        self.assertEqual((self.root / "features/assistant/payload.squashfs").read_bytes(), b"new")
        self.assertFalse((self.root / "features/assistant/runtime.squashfs").exists())
        self.assertFalse((self.root / "features/assistant/runtime-manifest.json").exists())
        self.assertFalse((self.root / "feature-commit").exists())

    def test_ambiguous_bcb_readback_fails_closed_and_keeps_staging(self) -> None:
        self.assertEqual(self.invoke("prepare-boot").returncode, 0)
        self.bcb.write_text("selected_slot=a\nslot_a_success=0\n")
        result = self.invoke("commit-after-confirm")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / "staging/features/assistant/libreecho-radar-puffin-0.13.11-assistant.payload.squashfs").exists())
        self.assertEqual((self.root / "features/assistant/payload.squashfs").read_bytes(), b"old")

    def test_prepare_recovers_after_each_durable_journal_boundary(self) -> None:
        for trigger in (1, 2):
            with self.subTest(journal_boundary=trigger):
                _, fault_env = self.faulting_busybox(trigger)
                crashed = self.invoke("prepare-boot", extra_env=fault_env)
                self.assertNotEqual(crashed.returncode, 0)
                prepared = self.invoke("prepare-boot")
                self.assertEqual(prepared.returncode, 0, prepared.stderr)
                self.assertEqual(self.invoke("commit-after-confirm").returncode, 0)
            self.tmp.cleanup()
            self.setUp()

    def faulting_busybox(self, trigger: int) -> tuple[Path, dict[str, str]]:
        wrapper = self.root / f"busybox-fault-{trigger}"
        count = self.root / f"busybox-count-{trigger}"
        fired = self.root / f"busybox-fired-{trigger}"
        count.write_text("0")
        wrapper.write_text(
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = mv ]; then\n"
            "  /bin/busybox \"$@\" || exit $?\n"
            f"  n=$(cat {count})\n"
            "  n=$((n + 1)); printf '%s\\n' \"$n\" > " + str(count) + "\n"
            f"  if [ \"$n\" = \"{trigger}\" ] && [ ! -e {fired} ]; then\n"
            f"    : > {fired}; exit 97\n"
            "  fi\n"
            "  exit 0\n"
            "fi\nexec /bin/busybox \"$@\"\n"
        )
        wrapper.chmod(0o755)
        return wrapper, {"BB": str(wrapper)}

    def test_recovery_after_each_commit_rename_boundary_is_hash_idempotent(self) -> None:
        """A crash after every commit rename must recover without blind replay."""
        for trigger in range(1, 8):
            with self.subTest(rename_boundary=trigger):
                self.assertEqual(self.invoke("prepare-boot").returncode, 0)
                wrapper, fault_env = self.faulting_busybox(trigger)
                crashed = self.invoke("commit-after-confirm", extra_env=fault_env)
                self.assertNotEqual(crashed.returncode, 0, (trigger, crashed.stdout, crashed.stderr))
                recovered = self.invoke("commit-after-confirm")
                self.assertEqual(recovered.returncode, 0, (trigger, recovered.stdout, recovered.stderr))
                self.assertEqual((self.root / "features/assistant/payload.squashfs").read_bytes(), b"new")
                self.assertEqual((self.root / "features/assistant/manifest.json").read_bytes(), b"manifest")
                self.assertTrue((self.root / "installed").is_file())
                self.assertFalse((self.root / "feature-commit").exists())
                self.assertFalse((self.root / "pending").exists())
                self.assertFalse((self.root / "staging").exists())
            self.tmp.cleanup()
            self.setUp()

    def test_abort_rejects_confirmed_and_ambiguous_transactions(self) -> None:
        self.assertEqual(self.invoke("prepare-boot").returncode, 0)
        journal = self.root / "feature-commit"
        journal.write_text(journal.read_text().replace("phase=prepared", "phase=confirmed"))
        confirmed = self.invoke("abort-before-activation")
        self.assertNotEqual(confirmed.returncode, 0)
        self.assertTrue(journal.exists())
        self.assertTrue((self.root / "pending").exists())

        self.tmp.cleanup()
        self.setUp()
        self.assertEqual(self.invoke("prepare-boot").returncode, 0)
        _, fault_env = self.faulting_busybox(2)
        crashed = self.invoke("commit-after-confirm", extra_env=fault_env)
        self.assertNotEqual(crashed.returncode, 0)
        ambiguous = self.invoke("abort-before-activation")
        self.assertNotEqual(ambiguous.returncode, 0)
        self.assertTrue((self.root / "feature-commit").exists())
        self.assertTrue((self.root / "staging/previous-assistant-payload.squashfs").exists())

    def test_symlinked_feature_component_fails_closed_before_prepare(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "libreecho-radar-puffin-0.13.11-assistant.payload.squashfs").write_bytes(b"new")
        (outside / "libreecho-radar-puffin-0.13.11-assistant.manifest.json").write_bytes(b"manifest")
        assistant = self.root / "staging/features/assistant"
        for child in assistant.iterdir():
            child.unlink()
        assistant.rmdir()
        assistant.symlink_to(outside, target_is_directory=True)
        result = self.invoke("prepare-boot")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "pending").exists())
        self.assertFalse((self.root / "feature-commit").exists())


class RuntimeHarnessTests(unittest.TestCase):
    """Execute the real shell entry points against only temporary host paths."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ota-v2-runtime-")
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.update_root = self.data / "libreecho/update"
        self.features = self.data / "libreecho/features"
        self.source_features = self.root / "source-features"
        self.parts = self.root / "partitions"
        self.proc = self.root / "proc"
        self.run_root = self.root / "run"
        self.var_run = self.root / "var/run"
        self.etc = self.root / "etc"
        for path in (self.update_root, self.features / "assistant", self.source_features / "assistant", self.parts,
                     self.proc / "self", self.proc / "net", self.run_root / "libreecho/features/assistant/root",
                     self.var_run, self.etc / "libreecho"):
            path.mkdir(parents=True, exist_ok=True)
        for feature_id in FEATURE_IDS[:-1]:
            (self.features / feature_id).mkdir(parents=True, exist_ok=True)
        (self.data / "libreecho/automatic-updates").write_text("channel=stable\n")
        (self.root / "packaged-channel").write_text("stable\n")
        (self.root / "profile").write_text("ota\n")
        self.old_payload = self.features / "assistant/payload.squashfs"
        self.old_manifest = self.features / "assistant/manifest.json"
        self.old_payload.write_bytes(b"old")
        self.old_manifest.write_bytes(b"oldmeta")
        self.base_payload_hash = hashlib.sha256(b"old").hexdigest()
        self.base_manifest_hash = hashlib.sha256(b"oldmeta").hexdigest()
        self.new_payload = b"new"
        self.new_manifest = b"newmeta"
        self.boot = b"ANDROID!" + bytes(BOOT_SIZE - 8)
        self.boot_hash = hashlib.sha256(self.boot).hexdigest()
        self.payload_name = "libreecho-radar-puffin-0.13.11-assistant.payload.squashfs"
        self.manifest_name = "libreecho-radar-puffin-0.13.11-assistant.manifest.json"
        (self.source_features / "assistant" / self.payload_name).write_bytes(self.new_payload)
        (self.source_features / "assistant" / self.manifest_name).write_bytes(self.new_manifest)
        record = feature("assistant", "replace")
        record.update({
            "base_payload_sha256": self.base_payload_hash,
            "base_manifest_sha256": self.base_manifest_hash,
            "asset": self.payload_name,
            "size": len(self.new_payload),
            "sha256": hashlib.sha256(self.new_payload).hexdigest(),
            "manifest_asset": self.manifest_name,
            "manifest_size": len(self.new_manifest),
            "manifest_sha256": hashlib.sha256(self.new_manifest).hexdigest(),
        })
        records = [record]
        for feature_id in FEATURE_IDS[:-1]:
            payload = f"{feature_id}-base-payload".encode()
            metadata = f"{feature_id}-base-manifest".encode()
            (self.features / feature_id / "payload.squashfs").write_bytes(payload)
            (self.features / feature_id / "manifest.json").write_bytes(metadata)
            preserved = feature(feature_id, "preserve")
            preserved.update({
                "base_payload_sha256": hashlib.sha256(payload).hexdigest(),
                "base_manifest_sha256": hashlib.sha256(metadata).hexdigest(),
            })
            records.append(preserved)
        self.manifest = manifest(records)
        self.manifest["service_profile"] = "diagnostic"
        self.manifest["boot_sha256"] = self.boot_hash
        self.package = self.root / "update.ota.tar"
        from feature_manifest import build_control_tar
        self.package.write_bytes(build_control_tar(self.manifest, self.boot, KEY))
        self.public_key = self.root / "public-key.hex"
        self.verify = write_real_verifier(self.root, self.public_key, SigningKey(KEY))
        self.bootctl_state = self.root / "bootctl-state"
        self.bootctl_state.write_text("a\n")
        self.bootctl = self.root / "bootctl"
        self.bootctl_log = self.root / "bootctl.log"
        self.bootctl.write_text("""#!/bin/sh
printf '%s %s\\n' "${1:-}" "${2:-}" >> "$BOOTCTL_LOG"
state=$(cat \"$STATE_FILE\")
case \"${1:-}\" in
 status)
   if [ \"$state\" = b ]; then echo selected_slot=b; echo inactive_slot=a; echo slot_b_success=1; echo slot_a_success=1;
   else echo selected_slot=a; echo inactive_slot=b; echo slot_a_success=1; echo slot_b_success=0; fi ;;
 activate) exit 0 ;;
 confirm) printf '%s\\n' \"$2\" > \"$STATE_FILE\" ;;
 *) exit 2 ;;
esac
"""); self.bootctl.chmod(0o755)
        for slot in ("a", "b"):
            (self.parts / f"boot_{slot}").write_bytes(bytes(BOOT_SIZE))
        (self.etc / "libreecho/service-profile").write_text("diagnostic\n")
        (self.etc / "libreecho/feature-policy").write_text("preserve\n")
        (self.proc / "cmdline").write_bytes(b"androidboot.slot_suffix=_b\\0")
        self.bcb = self.root / "bcb"
        self.bcb.write_text("selected_slot=b\nslot_b_success=0\nslot_a_success=1\n")
        self.mountinfo = self.proc / "self/mountinfo"
        (self.proc / "net/unix").write_text("Num RefCount Protocol Flags Type St Inode Path\n")
        self.env = os.environ.copy()
        self.env.update({
            "LIBREECHO_TEST_MODE": "1",
            "LIBREECHO_DATA_ROOT": str(self.data),
            "LIBREECHO_UPDATE_ROOT": str(self.update_root),
            "LIBREECHO_FEATURE_ROOT": str(self.features),
            "LIBREECHO_BOOT_PARTITION_DIR": str(self.parts),
            "LIBREECHO_PACKAGED_CHANNEL_FILE": str(self.root / "packaged-channel"),
            "LIBREECHO_CHANNEL_FILE": str(self.data / "libreecho/automatic-updates"),
            "LIBREECHO_PROFILE_FILE": str(self.root / "profile"),
            "LIBREECHO_VERIFY_BIN": str(self.verify),
            "LIBREECHO_PUBLIC_KEY": str(self.public_key),
            "LIBREECHO_TRANSACTION_TEST_MODE": "1",
            "LIBREECHO_BOOTCTL": str(self.bootctl),
            "LIBREECHO_BOOTCTL_STATE": str(self.bootctl_state),
            "LIBREECHO_FEATURE_TRANSACTION": str(TRANSACTION),
            "STATE_FILE": str(self.bootctl_state),
            "BOOTCTL_LOG": str(self.bootctl_log),
            "LIBREECHO_BCB_FILE": str(self.bcb),
            "LIBREECHO_PROC_ROOT": str(self.proc),
            "LIBREECHO_CMDLINE_FILE": str(self.proc / "cmdline"),
            "LIBREECHO_MOUNTINFO_FILE": str(self.mountinfo),
            "LIBREECHO_PROC_NET_UNIX": str(self.proc / "net/unix"),
            "LIBREECHO_VAR_RUN_ROOT": str(self.var_run),
            "LIBREECHO_ETC_ROOT": str(self.etc),
            "LIBREECHO_RUN_ROOT": str(self.run_root),
        })

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def invoke_updater(self, *args: str) -> subprocess.CompletedProcess[str]:
        transaction = transaction_fixture(self.root, self.env)
        updater = updater_fixture(self.root, self.env, transaction)
        return run(["/bin/busybox", "sh", str(updater), *args], env=self.env)

    def test_real_fetcher_downloads_control_and_assets_via_fake_https_then_installs(self) -> None:
        curl = self.root / "fake-curl"
        curl.write_text("""#!/bin/sh
out=; err=; headers=; url=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output) out=$2; shift 2 ;;
    --stderr) err=$2; shift 2 ;;
    --dump-header) headers=$2; shift 2 ;;
    --write-out) shift 2 ;;
    *) url=$1; shift ;;
  esac
done
case "$url" in
  *.ota.tar) src=$SOURCE_PACKAGE ;;
  *) name=${url##*/}; src=$SOURCE_FEATURES/assistant/$name ;;
esac
[ -f "$src" ] || exit 22
size=$(stat -c %s "$src") || exit 63
[ -z "$headers" ] || printf 'HTTP/1.1 200 OK\\r\\nContent-Length: %s\\r\\n\\r\\n' "$size" > "$headers"
cp "$src" "$out"
[ -z "$err" ] || printf 'curl: (0) fake transfer https://token@private/path\\n' > "$err"
[ -n "$headers" ] || printf '200'
"""); curl.chmod(0o755)
        config = self.root / "ota-source.conf"
        config.write_text("schema=1\ncheck_interval_seconds=3600\n")
        env = self.env | {
            "LIBREECHO_FETCH_ROOT": str(self.update_root),
            "LIBREECHO_UPDATE_BIN": str(TOOLS / "initramfs/libreecho-update"),
            "LIBREECHO_FETCH_PROFILE": str(self.root / "profile"),
            "LIBREECHO_FETCH_CONFIG": str(config),
            "LIBREECHO_FETCH_CURL": str(curl),
            "LIBREECHO_FETCH_CURL_STDERR": str(self.root / "curl.stderr"),
            "LIBREECHO_FETCH_PACKAGED_CHANNEL": str(self.root / "packaged-channel"),
            "LIBREECHO_ASSISTANT_PAYLOAD": str(self.old_payload),
            "LIBREECHO_ASSISTANT_MANIFEST": str(self.old_manifest),
            "SOURCE_PACKAGE": str(self.package),
            "SOURCE_FEATURES": str(self.source_features),
        }
        transaction = transaction_fixture(self.root, env)
        updater = updater_fixture(self.root, env, transaction)
        fetcher = fetch_fixture(self.root, env, updater, transaction)
        fetched = run(["/bin/busybox", "sh", str(fetcher), "install"], env=env)
        self.assertEqual(fetched.returncode, 0, fetched.stderr + fetched.stdout)
        self.mountinfo.write_text(
            f"36 25 0:32 / {self.run_root}/libreecho/features/assistant/root ro - "
            f"squashfs {self.update_root}/staging/features/assistant/{self.payload_name} ro\\n"
        )
        self.bootctl_state.write_text("b\n")
        confirmed = run(["/bin/busybox", "sh", str(updater), "confirm"], env=env)
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr + confirmed.stdout)
        events = self.bootctl_log.read_text().splitlines()
        confirm_index = events.index("confirm b")
        self.assertTrue(any(event.startswith("status") for event in events[:confirm_index]), events)
        self.assertLess(events.index("status ", 0), confirm_index)
        self.assertEqual(self.old_payload.read_bytes(), self.new_payload)
        self.assertEqual(self.old_manifest.read_bytes(), self.new_manifest)
        self.assertFalse((self.update_root / "staging").exists())
        self.assertFalse((self.update_root / "curl.stderr").exists())

    def test_fetch_resume_and_existing_downloads_do_not_redownload_or_write_boot(self) -> None:
        stage = self.update_root / "staging/features/assistant"
        stage.mkdir(parents=True, exist_ok=True)
        (stage / self.payload_name).write_bytes(self.new_payload)
        (stage / f"{self.manifest_name}.part").write_bytes(self.new_manifest[:3])
        curl = self.root / "fake-curl-resume"
        curl.write_text("""#!/bin/sh
out=; err=; headers=; url=
printf '%s\\n' "$*" >> "$CURL_LOG"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output) out=$2; shift 2 ;;
    --stderr) err=$2; shift 2 ;;
    --dump-header) headers=$2; shift 2 ;;
    --write-out|--continue-at) [ "$1" = --output ] && out=$2; [ "$1" = --stderr ] && err=$2; shift 2 ;;
    *) url=$1; shift ;;
  esac
done
case "$url" in
  *.ota.tar) src=$SOURCE_PACKAGE ;;
  *) name=${url##*/}; src=$SOURCE_FEATURES/assistant/$name ;;
esac
[ -f "$src" ] || exit 22
size=$(stat -c %s "$src") || exit 63
[ -z "$headers" ] || printf 'HTTP/1.1 200 OK\\r\\nContent-Length: %s\\r\\n\\r\\n' "$size" > "$headers"
cp "$src" "$out"
[ -z "$err" ] || : > "$err"
[ -n "$headers" ] || printf '200'
""")
        curl.chmod(0o755)
        config = self.root / "ota-source-resume.conf"
        config.write_text("schema=1\ncheck_interval_seconds=3600\n")

        curl_log = self.root / "curl-resume.log"
        env = self.env | {
            "LIBREECHO_FETCH_ROOT": str(self.update_root),
            "LIBREECHO_UPDATE_BIN": str(TOOLS / "initramfs/libreecho-update"),
            "LIBREECHO_FETCH_PROFILE": str(self.root / "profile"),
            "LIBREECHO_FETCH_CONFIG": str(config),
            "LIBREECHO_FETCH_CURL": str(curl),
            "LIBREECHO_FETCH_CURL_STDERR": str(self.root / "curl-resume.stderr"),
            "LIBREECHO_FETCH_PACKAGED_CHANNEL": str(self.root / "packaged-channel"),
            "LIBREECHO_ASSISTANT_PAYLOAD": str(self.old_payload),
            "LIBREECHO_ASSISTANT_MANIFEST": str(self.old_manifest),
            "SOURCE_PACKAGE": str(self.package),
            "SOURCE_FEATURES": str(self.source_features),
            "CURL_LOG": str(curl_log),
        }
        transaction = transaction_fixture(self.root, env)
        updater = updater_fixture(self.root, env, transaction)
        fetcher = fetch_fixture(self.root, env, updater, transaction)
        fetched = run(["/bin/busybox", "sh", str(fetcher), "check"], env=env)
        self.assertEqual(fetched.returncode, 0, fetched.stderr + fetched.stdout)
        log = curl_log.read_text()
        self.assertIn("--continue-at -", log)
        self.assertNotIn(self.payload_name, log)
        self.assertFalse(self.bootctl_log.exists())

    def test_control_part_contract_handles_complete_corrupt_partial_and_oversize(self) -> None:
        curl = self.root / "fake-curl-control-part"
        curl.write_text("""#!/bin/sh
out=; err=; headers=; url=; resume=; range=;
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output) out=$2; shift 2 ;;
    --stderr) err=$2; shift 2 ;;
    --dump-header) headers=$2; shift 2 ;;
    --write-out) shift 2 ;;
    --continue-at) resume=$2; shift 2 ;;
    --range) range=$2; shift 2 ;;
    *) url=$1; shift ;;
  esac
done
printf '%s\\n' "headers=$headers out=$out $* resume=$resume range=$range" >> "$CURL_LOG"
src=$SOURCE_PACKAGE
[ -f "$src" ] || exit 22
size=$(stat -c %s "$src") || exit 63
if [ "$range" = 0-0 ]; then
  printf 'HTTP/1.1 206 Partial Content\\r\\nContent-Range: bytes 0-0/%s\\r\\nContent-Length: 1\\r\\n\\r\\n' "$size" > "$headers"
  [ "$out" = /dev/null ] || printf x > "$out"
  exit 0
fi
if [ -n "$range" ]; then
  offset=${range%-}
  [ "$offset" -lt "$size" ] || { printf 'HTTP/1.1 416 Range Not Satisfiable\\r\\nContent-Range: bytes */%s\\r\\n\\r\\n' "$size" > "$headers"; exit 33; }
  printf 'HTTP/1.1 206 Partial Content\\r\\nContent-Range: bytes %s-%s/%s\\r\\nContent-Length: %s\\r\\n\\r\\n' "$offset" "$((size - 1))" "$size" "$((size - offset))" > "$headers"
  dd if="$src" bs=1 skip="$offset" of="$out" 2>/dev/null || exit 23
  exit 0
fi
if [ -n "$resume" ]; then
  offset=$(stat -c %s "$out") || exit 33
  [ "$offset" -lt "$size" ] || { printf 'HTTP/1.1 416 Range Not Satisfiable\\r\\nContent-Range: bytes */%s\\r\\n\\r\\n' "$size" > "$headers"; exit 33; }
  tail_file=$out.tail
  dd if="$src" bs=1 skip="$offset" of="$tail_file" 2>/dev/null || exit 23
  cat "$tail_file" >> "$out" || exit 23
  rm -f "$tail_file"
  exit 0
fi
[ "$size" -le 33554432 ] || exit 63
printf 'HTTP/1.1 200 OK\\r\\nContent-Length: %s\\r\\n\\r\\n' "$size" > "$headers"
cp "$src" "$out" || exit 23
""")
        curl.chmod(0o755)
        config = self.root / "ota-source-control-part.conf"
        config.write_text("schema=1\ncheck_interval_seconds=3600\n")
        env = self.env | {
            "LIBREECHO_FETCH_ROOT": str(self.update_root),
            "LIBREECHO_UPDATE_BIN": str(TOOLS / "initramfs/libreecho-update"),
            "LIBREECHO_FETCH_PROFILE": str(self.root / "profile"),
            "LIBREECHO_FETCH_CONFIG": str(config),
            "LIBREECHO_FETCH_CURL": str(curl),
            "LIBREECHO_FETCH_CURL_STDERR": str(self.root / "control-part.stderr"),
            "LIBREECHO_FETCH_CURL_HEADERS": str(self.root / "control-part.headers"),
            "LIBREECHO_FETCH_PACKAGED_CHANNEL": str(self.root / "packaged-channel"),
            "LIBREECHO_ASSISTANT_PAYLOAD": str(self.old_payload),
            "LIBREECHO_ASSISTANT_MANIFEST": str(self.old_manifest),
            "SOURCE_PACKAGE": str(self.package),
            "SOURCE_FEATURES": str(self.source_features),
            "CURL_LOG": str(self.root / "control-part.log"),
        }

        def reset_stage() -> None:
            shutil.rmtree(self.update_root / "incoming", ignore_errors=True)
            shutil.rmtree(self.update_root / "staging", ignore_errors=True)
            shutil.rmtree(self.update_root / "quarantine", ignore_errors=True)
            (self.update_root / "incoming").mkdir(parents=True)
            stage = self.update_root / "staging/features/assistant"
            stage.mkdir(parents=True)
            (stage / self.payload_name).write_bytes(self.new_payload)
            (stage / self.manifest_name).write_bytes(self.new_manifest)
            (self.root / "control-part.log").unlink(missing_ok=True)
            (self.root / "control-part.headers").unlink(missing_ok=True)
            (self.root / "control-part.stderr").unlink(missing_ok=True)

        def fetch() -> subprocess.CompletedProcess[str]:
            transaction = transaction_fixture(self.root, env)
            updater = updater_fixture(self.root, env, transaction)
            fetcher = fetch_fixture(self.root, env, updater, transaction)
            return run(["/bin/busybox", "sh", str(fetcher), "check"], env=env)

        reset_stage()
        (self.update_root / "incoming/github-update.ota.tar.part").write_bytes(self.package.read_bytes())
        complete = fetch()
        self.assertEqual(complete.returncode, 0, complete.stderr + complete.stdout)
        self.assertFalse((self.root / "control-part.log").exists())
        self.assertEqual((self.update_root / "incoming/github-update.ota.tar").read_bytes(), self.package.read_bytes())

        reset_stage()
        (self.update_root / "incoming/github-update.ota.tar.part").write_bytes(b"X" * self.package.stat().st_size)
        corrupt = fetch()
        self.assertEqual(corrupt.returncode, 0, corrupt.stderr + corrupt.stdout)
        self.assertIn("UPDATE_AVAILABLE version=0.13.11", corrupt.stdout)
        self.assertIn("range=0-0", (self.root / "control-part.log").read_text())
        self.assertNotIn("resume=--", (self.root / "control-part.log").read_text())
        self.assertTrue((self.update_root / "quarantine/github-update.ota.tar.part.bad").is_file())
        self.assertEqual((self.update_root / "incoming/github-update.ota.tar").read_bytes(), self.package.read_bytes())

        reset_stage()
        raw = self.package.read_bytes()
        (self.update_root / "incoming/github-update.ota.tar.part").write_bytes(raw[:len(raw) // 2])
        partial = fetch()
        self.assertEqual(partial.returncode, 0, partial.stderr + partial.stdout)
        log = (self.root / "control-part.log").read_text()
        self.assertIn("range=0-0", log)
        self.assertIn(f"range={len(raw) // 2}-", log)
        self.assertNotIn("resume=--continue-at", log)
        self.assertFalse((self.update_root / "quarantine").exists())
        self.assertEqual((self.update_root / "incoming/github-update.ota.tar").read_bytes(), raw)

        reset_stage()
        oversized = self.root / "oversized-control.ota.tar"
        with oversized.open("wb") as output:
            output.truncate(33554433)
        oversize_env = env | {"SOURCE_PACKAGE": str(oversized)}
        transaction = transaction_fixture(self.root, oversize_env)
        updater = updater_fixture(self.root, oversize_env, transaction)
        fetcher = fetch_fixture(self.root, oversize_env, updater, transaction)
        oversized_result = run(["/bin/busybox", "sh", str(fetcher), "check"], env=oversize_env)
        self.assertNotEqual(oversized_result.returncode, 0)
        self.assertIn("ERROR:download_size", oversized_result.stderr)
        self.assertFalse((self.update_root / "incoming/github-update.ota.tar").exists())
        part = self.update_root / "incoming/github-update.ota.tar.part"
        self.assertFalse(part.exists())

    def test_control_http200_range_ignore_falls_back_to_fresh_bounded_download(self) -> None:
        """A 200 probe must never cause a --continue-at resume attempt."""
        curl = self.root / "fake-curl-http200-ignore-range"
        curl.write_text("""#!/bin/sh
out=; err=; headers=; url=; range=; resume=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output) out=$2; shift 2 ;;
    --stderr) err=$2; shift 2 ;;
    --dump-header) headers=$2; shift 2 ;;
    --write-out) shift 2 ;;
    --range) range=$2; shift 2 ;;
    --continue-at) resume=$2; shift 2 ;;
    *) url=$1; shift ;;
  esac
done
printf '%s\\n' "range=$range resume=$resume" >> "$CURL_LOG"
size=$(stat -c %s "$SOURCE_PACKAGE") || exit 63
if [ -n "$range" ]; then
  printf 'HTTP/1.1 200 OK\\r\\nContent-Length: %s\\r\\n\\r\\n' "$size" > "$headers"
  exit 0
fi
[ -z "$resume" ] || exit 33
printf 'HTTP/1.1 200 OK\\r\\nContent-Length: %s\\r\\n\\r\\n' "$size" > "$headers"
cp "$SOURCE_PACKAGE" "$out" || exit 23
""")
        curl.chmod(0o755)
        config = self.root / "ota-source-http200.conf"
        config.write_text("schema=1\ncheck_interval_seconds=3600\n")
        env = self.env | {
            "LIBREECHO_FETCH_ROOT": str(self.update_root),
            "LIBREECHO_UPDATE_BIN": str(TOOLS / "initramfs/libreecho-update"),
            "LIBREECHO_FETCH_PROFILE": str(self.root / "profile"),
            "LIBREECHO_FETCH_CONFIG": str(config),
            "LIBREECHO_FETCH_CURL": str(curl),
            "LIBREECHO_FETCH_CURL_STDERR": str(self.root / "http200.stderr"),
            "LIBREECHO_FETCH_PACKAGED_CHANNEL": str(self.root / "packaged-channel"),
            "LIBREECHO_ASSISTANT_PAYLOAD": str(self.old_payload),
            "LIBREECHO_ASSISTANT_MANIFEST": str(self.old_manifest),
            "SOURCE_PACKAGE": str(self.package),
            "CURL_LOG": str(self.root / "http200.log"),
        }
        (self.update_root / "incoming").mkdir(parents=True, exist_ok=True)
        stage = self.update_root / "staging/features/assistant"
        stage.mkdir(parents=True, exist_ok=True)
        stage.joinpath(self.payload_name).write_bytes(self.new_payload)
        stage.joinpath(self.manifest_name).write_bytes(self.new_manifest)
        from feature_manifest import serialize_manifest
        (self.update_root / "staging/manifest").write_bytes(serialize_manifest(self.manifest))
        (self.update_root / "incoming/github-update.ota.tar.part").write_bytes(b"partial-control")
        transaction = transaction_fixture(self.root, env)
        updater = updater_fixture(self.root, env, transaction)
        fetcher = fetch_fixture(self.root, env, updater, transaction)
        result = run(["/bin/busybox", "sh", str(fetcher), "check"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        log = (self.root / "http200.log").read_text()
        self.assertIn("range=0-0 resume=", log)
        self.assertIn("range= resume=", log)
        self.assertNotIn("resume=--continue-at", log)
        self.assertEqual(
            (self.update_root / "incoming/github-update.ota.tar").read_bytes(),
            self.package.read_bytes(),
        )

    def test_control_http_response_matrix_enforces_final_headers_and_stream_cap(self) -> None:
        curl = self.root / "fake-curl-control-response-matrix"
        curl.write_text("""#!/bin/sh
out=; headers=; url=; range=;
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output) out=$2; shift 2 ;;
    --stderr) shift 2 ;;
    --dump-header) headers=$2; shift 2 ;;
    --range) range=$2; shift 2 ;;
    --write-out) shift 2 ;;
    *) url=$1; shift ;;
  esac
done
printf '%s\\n' "scenario=$SCENARIO range=$range" >> "$CURL_LOG"
src=$SOURCE_PACKAGE
size=$(stat -c %s "$src") || exit 63
header_200() { if [ "$SCENARIO" = 200-nolength ]; then printf 'HTTP/1.1 200 OK\\r\\n\\r\\n' > "$headers"; else printf 'HTTP/1.1 200 OK\\r\\nContent-Length: %s\\r\\n\\r\\n' "$size" > "$headers"; fi; }
if [ "$range" = 0-0 ]; then
  case "$SCENARIO" in
    206|redirect206) [ "$SCENARIO" = redirect206 ] && printf 'HTTP/1.1 302 Found\\r\\nLocation: https://redirect.invalid/ota\\r\\n\\r\\n' > "$headers"; printf 'HTTP/1.1 206 Partial Content\\r\\nContent-Range: bytes 0-0/%s\\r\\nContent-Length: 1\\r\\n\\r\\n' "$size" >> "$headers"; printf x > "$out"; exit 0 ;;
    malformed206) printf 'HTTP/1.1 206 Partial Content\\r\\nContent-Range: bytes 1-0/%s\\r\\nContent-Length: 1\\r\\n\\r\\n' "$size" > "$headers"; printf x > "$out"; exit 0 ;;
    contradictory206) printf 'HTTP/1.1 206 Partial Content\\r\\nContent-Range: bytes 0-0/%s\\r\\nContent-Range: bytes 1-1/%s\\r\\nContent-Length: 1\\r\\n\\r\\n' "$size" "$size" > "$headers"; printf x > "$out"; exit 0 ;;
    416) printf 'HTTP/1.1 416 Range Not Satisfiable\\r\\nContent-Range: bytes */%s\\r\\n\\r\\n' "$size" > "$headers"; exit 33 ;;
    200-length|200-nolength|oversized-chunked|oversized-length) header_200; exit 0 ;;
    interrupted|transport-tls) [ "$SCENARIO" = transport-tls ] && exit 60; printf 'HTTP/1.1 206 Partial Content\\r\\nContent-Range: bytes 0-0/%s\\r\\nContent-Length: 1\\r\\n\\r\\n' "$size" > "$headers"; printf x > "$out"; exit 0 ;;
  esac
fi
if [ -n "$range" ]; then
  offset=${range%-}
  case "$SCENARIO" in
    206|redirect206) printf 'HTTP/1.1 206 Partial Content\\r\\nContent-Range: bytes %s-%s/%s\\r\\nContent-Length: %s\\r\\n\\r\\n' "$offset" "$((size - 1))" "$size" "$((size - offset))" > "$headers"; dd if="$src" bs=1 skip="$offset" of="$out" 2>/dev/null; exit 0 ;;
    interrupted) printf 'HTTP/1.1 206 Partial Content\\r\\nContent-Range: bytes %s-%s/%s\\r\\nContent-Length: %s\\r\\n\\r\\n' "$offset" "$((size - 1))" "$size" "$((size - offset))" > "$headers"; dd if="$src" bs=1 skip="$offset" count=3 of="$out" 2>/dev/null; exit 28 ;;
    resume-malformed) printf 'HTTP/1.1 206 Partial Content\\r\\nContent-Range: malformed\\r\\nContent-Length: 3\\r\\n\\r\\n' > "$headers"; printf xxx > "$out"; exit 0 ;;
    200-length|200-nolength) header_200; cat "$src" > "$out"; exit 0 ;;
  esac
fi
case "$SCENARIO" in
  fresh-malformed) printf 'HTTP/1.1 200 OK\\r\\nContent-Length: 1\\r\\nContent-Length: 2\\r\\n\\r\\n' > "$headers"; printf x > "$out"; exit 0 ;;
  fresh-classification) printf 'HTTP/1.1 302 Found\\r\\nLocation: https://redirect.invalid/ota\\r\\n\\r\\n' > "$headers"; printf x > "$out"; exit 0 ;;
  fresh-transport-tls) exit 60 ;;
  signal) printf 'HTTP/1.1 200 OK\\r\\nContent-Length: 33554432\\r\\n\\r\\n' > "$headers"; i=0; while [ "$i" -lt 1000 ]; do head -c 65536 /dev/zero >> "$out" || exit 23; i=$((i + 1)); sleep 1; done; exit 0 ;;
  oversized-chunked) printf 'HTTP/1.1 200 OK\\r\\n\\r\\n' > "$headers"; head -c 33554433 /dev/zero > "$out"; exit $? ;;
  oversized-length) printf 'HTTP/1.1 200 OK\\r\\nContent-Length: 33554433\\r\\n\\r\\n' > "$headers"; head -c 33554433 /dev/zero > "$out"; exit $? ;;
  *) header_200; cat "$src" > "$out"; exit 0 ;;
esac
""")
        curl.chmod(0o755)
        config = self.root / "ota-source-control-matrix.conf"
        config.write_text("schema=1\ncheck_interval_seconds=3600\n")
        base_env = self.env | {
            "LIBREECHO_FETCH_ROOT": str(self.update_root),
            "LIBREECHO_UPDATE_BIN": str(TOOLS / "initramfs/libreecho-update"),
            "LIBREECHO_FETCH_PROFILE": str(self.root / "profile"),
            "LIBREECHO_FETCH_CONFIG": str(config),
            "LIBREECHO_FETCH_CURL": str(curl),
            "LIBREECHO_FETCH_STDERR": str(self.root / "matrix.stderr"),
            "LIBREECHO_FETCH_CURL_STDERR": str(self.root / "matrix.stderr"),
            "LIBREECHO_FETCH_PACKAGED_CHANNEL": str(self.root / "packaged-channel"),
            "LIBREECHO_ASSISTANT_PAYLOAD": str(self.old_payload),
            "LIBREECHO_ASSISTANT_MANIFEST": str(self.old_manifest),
            "SOURCE_PACKAGE": str(self.package),
            "CURL_LOG": str(self.root / "matrix.log"),
        }

        def reset_stage() -> None:
            shutil.rmtree(self.update_root / "incoming", ignore_errors=True)
            shutil.rmtree(self.update_root / "staging", ignore_errors=True)
            shutil.rmtree(self.update_root / "quarantine", ignore_errors=True)
            (self.update_root / "incoming").mkdir(parents=True)
            stage = self.update_root / "staging/features/assistant"
            stage.mkdir(parents=True)
            stage.joinpath(self.payload_name).write_bytes(self.new_payload)
            stage.joinpath(self.manifest_name).write_bytes(self.new_manifest)
            from feature_manifest import serialize_manifest
            (self.update_root / "staging/manifest").write_bytes(serialize_manifest(self.manifest))

        def assert_no_control_temps() -> None:
            prefixes = (".control-fresh.", ".control-resume.", ".control-range-probe.")
            leftovers = [path for path in self.update_root.iterdir() if path.name.startswith(prefixes)]
            leftovers += [
                path for path in (self.root / "control-part.headers", self.root / "curl.headers", self.root / "matrix.stderr")
                if path.exists() or path.is_symlink()
            ]
            self.assertEqual(leftovers, [], leftovers)

        def run_case(mode: str, part: bytes | None) -> subprocess.CompletedProcess[str]:
            reset_stage()
            if part is not None:
                (self.update_root / "incoming/github-update.ota.tar.part").write_bytes(part)
            env = base_env | {"SCENARIO": mode}
            transaction = transaction_fixture(self.root, env)
            updater = updater_fixture(self.root, env, transaction)
            fetcher = fetch_fixture(self.root, env, updater, transaction)
            return run(["/bin/busybox", "sh", str(fetcher), "check"], env=env)

        raw = self.package.read_bytes()
        for mode in ("206", "redirect206", "200-length", "200-nolength", "416"):
            result = run_case(mode, raw[: len(raw) // 2] if mode != "416" else b"corrupt")
            self.assertEqual(result.returncode, 0, mode + ": " + result.stderr + result.stdout)
            self.assertEqual((self.update_root / "incoming/github-update.ota.tar").read_bytes(), raw)
            if mode == "416": self.assertTrue((self.update_root / "quarantine/github-update.ota.tar.part.bad").is_file())
            assert_no_control_temps()
        for mode in ("malformed206", "contradictory206", "interrupted", "transport-tls", "oversized-chunked", "oversized-length"):
            original = b"partial-control"
            result = run_case(mode, original)
            self.assertNotEqual(result.returncode, 0, mode)
            self.assertEqual((self.update_root / "incoming/github-update.ota.tar.part").read_bytes(), original, mode)
            self.assertFalse((self.update_root / "incoming/github-update.ota.tar").exists(), mode)
            assert_no_control_temps()
        for mode in ("fresh-malformed", "fresh-classification", "fresh-transport-tls", "oversized-chunked", "oversized-length"):
            result = run_case(mode, None)
            self.assertNotEqual(result.returncode, 0, mode)
            self.assertFalse((self.update_root / "incoming/github-update.ota.tar").exists(), mode)
            assert_no_control_temps()
        for _ in range(3):
            result = run_case("fresh-malformed", None)
            self.assertNotEqual(result.returncode, 0)
            assert_no_control_temps()
        result = run_case("resume-malformed", raw[: len(raw) // 2])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.update_root / "incoming/github-update.ota.tar.part").read_bytes(), raw[: len(raw) // 2])
        assert_no_control_temps()
        self.assertNotIn("continue-at", (self.root / "matrix.log").read_text())

        fake_df = self.root / "fake-df-lowspace"
        fake_df.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
            "printf '%s\\n' 'fixture 999999 0 0 100% /data'\n"
        )
        fake_df.chmod(0o755)
        boot_before = (self.parts / "boot_b").read_bytes()
        bcb_before = self.bcb.read_text()
        result = run([
            "/bin/busybox", "sh", str(TOOLS / "initramfs/libreecho-update"),
            "install", str(self.package), "--feature-dir", str(self.source_features),
        ], env=self.env | {"LIBREECHO_TRANSACTION_DF_BIN": str(fake_df)})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.parts / "boot_b").read_bytes(), boot_before)
        self.assertEqual(self.bcb.read_text(), bcb_before)
        self.assertFalse((self.update_root / "pending").exists())
        self.assertFalse((self.update_root / "feature-commit").exists())
        if self.bootctl_log.exists():
            events = self.bootctl_log.read_text()
            self.assertNotIn("activate", events)
            self.assertNotIn("confirm", events)

    def test_control_temp_cleanup_reaps_owned_children_on_int_and_term(self) -> None:
        curl = self.root / "fake-curl-control-signal"
        curl.write_text("""#!/bin/sh
out=; headers=; url=; range=;
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output) out=$2; shift 2 ;;
    --stderr) shift 2 ;;
    --dump-header) headers=$2; shift 2 ;;
    --range) range=$2; shift 2 ;;
    --write-out) shift 2 ;;
    *) url=$1; shift ;;
  esac
done
[ -z "$range" ] || exit 60
printf 'HTTP/1.1 200 OK\\r\\nContent-Length: 33554432\\r\\n\\r\\n' > "$headers"
i=0
while [ "$i" -lt 1000 ]; do
  head -c 65536 /dev/zero >> "$out" || exit 23
  i=$((i + 1))
  sleep 1
done
""")
        curl.chmod(0o755)
        config = self.root / "ota-source-control-signal.conf"
        config.write_text("schema=1\ncheck_interval_seconds=3600\n")
        env = self.env | {
            "LIBREECHO_FETCH_ROOT": str(self.update_root),
            "LIBREECHO_UPDATE_BIN": str(TOOLS / "initramfs/libreecho-update"),
            "LIBREECHO_FETCH_PROFILE": str(self.root / "profile"),
            "LIBREECHO_FETCH_CONFIG": str(config),
            "LIBREECHO_FETCH_CURL": str(curl),
            "LIBREECHO_FETCH_CURL_STDERR": str(self.root / "signal.stderr"),
            "LIBREECHO_FETCH_CURL_HEADERS": str(self.root / "signal.headers"),
            "LIBREECHO_FETCH_PACKAGED_CHANNEL": str(self.root / "packaged-channel"),
            "LIBREECHO_ASSISTANT_PAYLOAD": str(self.old_payload),
            "LIBREECHO_ASSISTANT_MANIFEST": str(self.old_manifest),
            "SOURCE_PACKAGE": str(self.package),
            "CURL_LOG": str(self.root / "signal.log"),
        }
        stale_manifest = self.update_root / ".control-owned.999999"
        stale_fresh = self.update_root / ".control-fresh.999999"
        stale_capture = self.update_root / ".control-fresh.999999.capture"
        stale_fifo = self.update_root / ".control-fresh.999999.fifo"
        stale_headers = self.root / "signal.headers"
        stale_stderr = self.root / "signal.stderr"
        stale_target = self.root / "stale-target"
        stale_target.write_text("must-survive")
        stale_link = self.update_root / ".control-fresh.999999.capture-link"
        stale_fresh.write_text("stale")
        stale_capture.write_text("stale")
        os.mkfifo(stale_fifo)
        stale_link.symlink_to(stale_target)
        stale_headers.write_text("stale")
        stale_stderr.write_text("stale")
        stale_manifest.write_text("\n".join(map(str, (stale_fresh, stale_capture, stale_fifo, stale_headers, stale_stderr))) + "\n")
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig):
                shutil.rmtree(self.update_root / "incoming", ignore_errors=True)
                (self.update_root / "incoming").mkdir(parents=True)
                transaction = transaction_fixture(self.root, env)
                updater = updater_fixture(self.root, env, transaction)
                fetcher = fetch_fixture(self.root, env, updater, transaction)
                process = subprocess.Popen(
                    ["/bin/busybox", "sh", str(fetcher), "check"],
                    env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                time.sleep(0.2)
                os.killpg(process.pid, sig)
                stdout, stderr = process.communicate(timeout=10)
                self.assertNotEqual(process.returncode, 0, (stdout, stderr))
                leftovers = [
                    path for path in self.update_root.iterdir()
                    if path.name.startswith((".control-fresh.", ".control-resume.", ".control-range-probe."))
                    and path != stale_link
                ]
                leftovers += [
                    path for path in (self.root / "signal.headers", self.root / "signal.stderr")
                    if path.exists() or path.is_symlink()
                ]
                self.assertEqual(leftovers, [], leftovers)
                self.assertTrue(stale_link.is_symlink())
                self.assertEqual(stale_target.read_text(), "must-survive")

    def test_stale_manifest_cleanup_rejects_path_pivots_and_malformed_records(self) -> None:
        """Stale cleanup must only unlink the exact owner-derived artifact set."""
        curl = self.root / "fake-curl-stale-cleanup"
        curl.write_text("#!/bin/sh\nexit 6\n")
        curl.chmod(0o755)
        config = self.root / "ota-source-stale-cleanup.conf"
        config.write_text("schema=1\ncheck_interval_seconds=3600\n")
        env = self.env | {
            "LIBREECHO_FETCH_ROOT": str(self.update_root),
            "LIBREECHO_UPDATE_BIN": str(TOOLS / "initramfs/libreecho-update"),
            "LIBREECHO_FETCH_PROFILE": str(self.root / "profile"),
            "LIBREECHO_FETCH_CONFIG": str(config),
            "LIBREECHO_FETCH_CURL": str(curl),
            "LIBREECHO_FETCH_CURL_STDERR": str(self.root / "stale-cleanup.stderr"),
            "LIBREECHO_FETCH_CURL_HEADERS": str(self.root / "stale-cleanup.headers"),
            "LIBREECHO_FETCH_PACKAGED_CHANNEL": str(self.root / "packaged-channel"),
            "LIBREECHO_ASSISTANT_PAYLOAD": str(self.old_payload),
            "LIBREECHO_ASSISTANT_MANIFEST": str(self.old_manifest),
            "SOURCE_PACKAGE": str(self.package),
        }
        transaction = transaction_fixture(self.root, env)
        updater = updater_fixture(self.root, env, transaction)
        fetcher = fetch_fixture(self.root, env, updater, transaction)

        def invoke() -> subprocess.CompletedProcess[str]:
            return run(["/bin/busybox", "sh", str(fetcher), "check"], env=env)

        outside = self.root / "outside"
        outside.mkdir()
        victim = outside / "victim"
        victim.write_text("must-survive")
        pivot = self.update_root / ".control-fresh.424242"
        pivot.symlink_to(outside, target_is_directory=True)
        manifest_path = self.update_root / ".control-owned.424242"
        manifest_path.write_text(f"{pivot}/victim\n")
        result = invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(victim.exists(), result.stderr)
        self.assertTrue(pivot.is_symlink())
        manifest_path.unlink()

        canonical_part = self.update_root / "incoming/github-update.ota.tar.part"
        canonical_part.parent.mkdir(parents=True, exist_ok=True)
        canonical_part.write_text("must-survive")
        quarantine = self.update_root / "quarantine/github-update.ota.tar.part.bad"
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        quarantine.write_text("must-survive")
        other_owner = self.update_root / ".control-fresh.999999"
        other_owner.write_text("must-survive")
        valid_regular = self.update_root / ".control-resume.424243"
        valid_regular.write_text("remove-me")
        valid_fifo = self.update_root / ".control-range-probe.424243.fifo"
        os.mkfifo(valid_fifo)
        direct_target = outside / "direct-target"
        direct_target.write_text("must-survive")
        direct_symlink = self.update_root / ".control-fresh.424243"
        direct_symlink.symlink_to(direct_target)
        valid_manifest = self.update_root / ".control-owned.424243"
        valid_manifest.write_text("\n".join(map(str, (valid_regular, valid_fifo, direct_symlink))) + "\n")
        self.assertNotEqual(invoke().returncode, 0)
        self.assertFalse(valid_regular.exists())
        self.assertFalse(valid_fifo.exists())
        self.assertTrue(direct_symlink.is_symlink())
        self.assertTrue(direct_target.exists())
        self.assertTrue(canonical_part.exists())
        self.assertTrue(quarantine.exists())
        self.assertTrue(other_owner.exists())

        wrong_owner = self.update_root / ".control-fresh.111111"
        wrong_owner.write_text("must-survive")
        wrong_manifest = self.update_root / ".control-owned.424244"
        wrong_manifest.write_text(f"{wrong_owner}\n")
        self.assertNotEqual(invoke().returncode, 0)
        self.assertTrue(wrong_owner.exists())
        self.assertTrue(wrong_manifest.exists())
        wrong_manifest.unlink()

        malformed_suffix = self.update_root / ".control-fresh.424245.bad"
        malformed_suffix.write_text("must-survive")
        malformed_manifest = self.update_root / ".control-owned.424245"
        malformed_manifest.write_text(f"{malformed_suffix}\n")
        self.assertNotEqual(invoke().returncode, 0)
        self.assertTrue(malformed_suffix.exists())
        self.assertTrue(malformed_manifest.exists())
        malformed_manifest.unlink()

        traversal_victim = self.root / "traversal-victim"
        traversal_victim.write_text("must-survive")
        traversal_manifest = self.update_root / ".control-owned.424246"
        traversal_manifest.write_text(
            f"{self.update_root}/{os.path.relpath(traversal_victim, self.update_root)}\n"
        )
        self.assertNotEqual(invoke().returncode, 0)
        self.assertTrue(traversal_victim.exists())
        self.assertTrue(traversal_manifest.exists())
        traversal_manifest.unlink()

        bounded_regular = self.update_root / ".control-fresh.424247"
        bounded_regular.write_text("must-survive")
        bounded_manifest = self.update_root / ".control-owned.424247"
        bounded_manifest.write_text(f"{bounded_regular}\n" + "pid:1\n" * 64)
        self.assertNotEqual(invoke().returncode, 0)
        self.assertTrue(bounded_regular.exists())
        self.assertTrue(bounded_manifest.exists())
        bounded_manifest.unlink()

        sized_regular = self.update_root / ".control-fresh.424248"
        sized_regular.write_text("must-survive")
        sized_manifest = self.update_root / ".control-owned.424248"
        sized_manifest.write_text("x" * 4097)
        self.assertNotEqual(invoke().returncode, 0)
        self.assertTrue(sized_regular.exists())
        self.assertTrue(sized_manifest.exists())
        sized_manifest.unlink()

        locked_regular = self.update_root / ".control-fresh.424249"
        locked_regular.write_text("must-survive")
        locked_manifest = self.update_root / ".control-owned.424249"
        locked_manifest.write_text(f"{locked_regular}\n")
        lock = self.update_root / "fetch.lock"
        lock.mkdir()
        locked = invoke()
        self.assertNotEqual(locked.returncode, 0)
        self.assertIn("ERROR:download_busy", locked.stderr)
        self.assertTrue(locked_regular.exists())
        self.assertTrue(locked_manifest.exists())
        lock.rmdir()
        locked_manifest.unlink()

    def test_failed_v2_boot_write_aborts_transaction_without_touching_canonical_files(self) -> None:
        failing_busybox = self.root / "busybox-fail-boot-write"
        failing_busybox.write_text(
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = dd ]; then\n"
            "  for arg do case \"$arg\" in of=*) case \"${arg#of=}\" in *boot_b|*mmcblk0p11) exit 1;; esac;; esac; done\n"
            "fi\n"
            "exec /bin/busybox \"$@\"\n"
        )
        failing_busybox.chmod(0o755)
        env = self.env | {
            "LIBREECHO_BUSYBOX": str(failing_busybox),
            "BB": str(failing_busybox),
        }
        transaction = transaction_fixture(self.root, env)
        updater = updater_fixture(self.root, env, transaction)
        result = run([
            "/bin/busybox", "sh", str(updater),
            "install", str(self.package), "--feature-dir", str(self.source_features),
        ], env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.old_payload.read_bytes(), b"old")
        self.assertEqual(self.old_manifest.read_bytes(), b"oldmeta")
        self.assertFalse((self.update_root / "pending").exists())
        self.assertFalse((self.update_root / "feature-commit").exists())
        self.assertFalse((self.update_root / "staging").exists())
        self.assertIn("state=failed", (self.update_root / "state").read_text())


class PreConfirmAcceptanceTests(unittest.TestCase):
    """Exercise verify-running with real scripts and private runtime fixtures."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ota-v2-preconfirm-")
        self.root = Path(self.tmp.name)
        self.update = self.root / "update"
        self.staging = self.update / "staging"
        self.features = self.root / "features"
        self.proc = self.root / "proc"
        self.run_root = self.root / "run"
        self.var_run = self.root / "var/run"
        self.etc = self.root / "etc"
        self.parts = self.root / "parts"
        for path in (
            self.staging / "features/airplay2", self.features / "airplay2",
            self.proc / "self", self.proc / "net", self.proc / "123",
            self.run_root / "libreecho/features/airplay2/root",
            self.var_run, self.etc / "libreecho", self.parts,
        ):
            path.mkdir(parents=True, exist_ok=True)
        for feature_id in FEATURE_IDS[1:]:
            (self.features / feature_id).mkdir(parents=True, exist_ok=True)

        self.old_payload = self.features / "airplay2/payload.squashfs"
        self.old_manifest = self.features / "airplay2/manifest.json"
        self.old_payload.write_bytes(b"old-base")
        self.old_manifest.write_bytes(b"old-base-manifest")
        self.payload_name = "libreecho-radar-puffin-0.13.11-airplay2.payload.squashfs"
        self.manifest_name = "libreecho-radar-puffin-0.13.11-airplay2.manifest.json"
        self.new_payload = self.staging / "features/airplay2" / self.payload_name
        self.new_manifest = self.staging / "features/airplay2" / self.manifest_name
        self.new_payload.write_bytes(b"candidate-payload")
        self.new_manifest.write_bytes(b"candidate-manifest")
        self.daemon = self.root / "candidate-daemon"
        self.daemon.write_bytes(b"candidate-daemon-binary")
        self.daemon_hash = hashlib.sha256(self.daemon.read_bytes()).hexdigest()
        self.boot = b"ANDROID!" + bytes(BOOT_SIZE - 8)
        self.boot_hash = hashlib.sha256(self.boot).hexdigest()
        (self.parts / "boot_b").write_bytes(self.boot)
        (self.parts / "boot_a").write_bytes(bytes(BOOT_SIZE))

        self.socket = self.run_root / "libreecho/airplay.sock"
        import socket
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket))
        listener.listen(1)
        self.listener = listener
        (self.proc / "net/unix").write_text(
            f"Num RefCount Protocol Flags Type St Inode Path\n"
            f"00000000: 00000002 00000000 00010000 0001 01 123 {self.socket}\n"
        )
        (self.proc / "cmdline").write_bytes(b"console=tty0 androidboot.slot_suffix=_b\0")
        (self.proc / "123/exe").symlink_to(self.daemon)
        (self.var_run / "libreecho-airplayd.pid").write_text("123\n")
        (self.etc / "libreecho/service-profile").write_text("production\n")
        (self.etc / "libreecho/feature-policy").write_text("redistributable\n")
        (self.root / "config.json").write_text('{"integrations":17}\n')
        (self.root / "data-config").mkdir(parents=True)
        self.mountinfo = self.proc / "self/mountinfo"

        record = feature("airplay2", "replace")
        record.update({
            "base_payload_sha256": hashlib.sha256(self.old_payload.read_bytes()).hexdigest(),
            "base_manifest_sha256": hashlib.sha256(self.old_manifest.read_bytes()).hexdigest(),
            "daemon_sha256": self.daemon_hash,
            "asset": self.new_payload.name,
            "size": self.new_payload.stat().st_size,
            "sha256": hashlib.sha256(self.new_payload.read_bytes()).hexdigest(),
            "manifest_asset": self.new_manifest.name,
            "manifest_size": self.new_manifest.stat().st_size,
            "manifest_sha256": hashlib.sha256(self.new_manifest.read_bytes()).hexdigest(),
        })
        records = [record]
        for feature_id in FEATURE_IDS[1:]:
            payload = f"{feature_id}-base-payload".encode()
            metadata = f"{feature_id}-base-manifest".encode()
            (self.features / feature_id / "payload.squashfs").write_bytes(payload)
            (self.features / feature_id / "manifest.json").write_bytes(metadata)
            preserved = feature(feature_id, "preserve")
            preserved.update({
                "base_payload_sha256": hashlib.sha256(payload).hexdigest(),
                "base_manifest_sha256": hashlib.sha256(metadata).hexdigest(),
            })
            records.append(preserved)
        self.manifest = manifest(records)
        self.manifest["feature_policy"] = "redistributable"
        self.manifest["boot_sha256"] = self.boot_hash
        self.manifest_path = self.staging / "manifest"
        from feature_manifest import serialize_manifest
        self.manifest_path.write_bytes(serialize_manifest(self.manifest))
        self.signing_key = SigningKey.generate()
        self.public_key = self.root / "public-key.hex"
        self.verify = write_real_verifier(self.root, self.public_key, self.signing_key)
        (self.staging / "manifest.sig").write_text(
            self.signing_key.sign(self.manifest_path.read_bytes()).signature.hex() + "\n"
        )
        self.bcb = self.root / "bcb"
        self.bcb.write_text("selected_slot=b\nslot_b_success=0\nslot_a_success=1\n")

        self.env = os.environ.copy()
        self.env.update({
            "LIBREECHO_TRANSACTION_ROOT": str(self.update),
            "LIBREECHO_TRANSACTION_STAGING": str(self.staging),
            "LIBREECHO_FEATURE_ROOT": str(self.features),
            "LIBREECHO_TRANSACTION_SLOT": "b",
            "LIBREECHO_BCB_FILE": str(self.bcb),
            "LIBREECHO_BOOT_PARTITION_DIR": str(self.parts),
            "LIBREECHO_PROC_ROOT": str(self.proc),
            "LIBREECHO_CMDLINE_FILE": str(self.proc / "cmdline"),
            "LIBREECHO_MOUNTINFO_FILE": str(self.mountinfo),
            "LIBREECHO_PROC_NET_UNIX": str(self.proc / "net/unix"),
            "LIBREECHO_VAR_RUN_ROOT": str(self.var_run),
            "LIBREECHO_ETC_ROOT": str(self.etc),
            "LIBREECHO_RUN_ROOT": str(self.run_root),
            "LIBREECHO_FEATURE_CONFIG": str(self.root / "config.json"),
            "LIBREECHO_VERIFY_BIN": str(self.verify),
            "LIBREECHO_PUBLIC_KEY": str(self.public_key),
            "LIBREECHO_TRANSACTION_TEST_MODE": "1",
            "LIBREECHO_TRANSACTION_SLOT": "b",
        })

    def tearDown(self) -> None:
        self.listener.close()
        self.tmp.cleanup()

    def prepare(self) -> None:
        prepared = run([str(transaction_fixture(self.root, self.env)), "prepare-boot"], env=self.env)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.mountinfo.write_text(
            f"36 25 0:32 / {self.run_root}/libreecho/features/airplay2/root ro - "
            f"squashfs {self.new_payload} ro\n"
        )

    def test_verify_running_accepts_selected_unconfirmed_slot_and_real_candidate(self) -> None:
        self.prepare()
        result = run([str(transaction_fixture(self.root, self.env)), "verify-running"], env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("running_verified slot=b", result.stdout)

    def test_verify_running_rejects_wrong_mount_without_mutating_canonical_feature(self) -> None:
        self.prepare()
        self.mountinfo.write_text(
            f"36 25 0:32 / {self.run_root}/libreecho/features/airplay2/root ro - "
            f"squashfs {self.old_payload} ro\n"
        )
        result = run([str(transaction_fixture(self.root, self.env)), "verify-running"], env=self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.old_payload.read_bytes(), b"old-base")
        self.assertEqual(self.bcb.read_text().splitlines()[1], "slot_b_success=0")

    def test_verify_running_rejects_wrong_base_without_mutating_canonical_feature(self) -> None:
        self.prepare()
        self.old_payload.write_bytes(b"wrong-base")
        result = run([str(transaction_fixture(self.root, self.env)), "verify-running"], env=self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.bcb.read_text().splitlines()[1], "slot_b_success=0")

    def test_verify_running_rejects_missing_socket_and_wrong_executable_hash(self) -> None:
        self.prepare()
        self.socket.unlink()
        result = run([str(transaction_fixture(self.root, self.env)), "verify-running"], env=self.env)
        self.assertNotEqual(result.returncode, 0)
        self.socket.parent.mkdir(parents=True, exist_ok=True)
        listener = __import__("socket").socket(__import__("socket").AF_UNIX, __import__("socket").SOCK_STREAM)
        listener.bind(str(self.socket)); listener.listen(1)
        self.listener.close(); self.listener = listener
        self.daemon.write_bytes(b"wrong-daemon")
        result = run([str(transaction_fixture(self.root, self.env)), "verify-running"], env=self.env)
        self.assertNotEqual(result.returncode, 0)

    def test_tampered_signed_manifest_is_rejected_before_activation_mount(self) -> None:
        self.prepare()
        self.manifest_path.write_bytes(self.manifest_path.read_bytes() + b"unknown_field=1\n")
        mount_log = self.root / "mount.log"
        busybox = self.root / "busybox"
        busybox.write_text(
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = mount ]; then printf '%s\\n' \"$*\" >> \"$MOUNT_LOG\"; exit 0; fi\n"
            "exec /bin/busybox \"$@\"\n"
        )
        busybox.chmod(0o755)
        env = self.env | {"BB": str(busybox), "MOUNT_LOG": str(mount_log)}
        result = run([str(transaction_fixture(self.root, env)), "activate-mounts"], env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manifest-signature", result.stderr)
        self.assertFalse(mount_log.exists())
        self.assertEqual(self.old_payload.read_bytes(), b"old-base")
        self.assertIn("slot_b_success=0", self.bcb.read_text())


class CommittedRuntimeLifecycleTests(unittest.TestCase):
    """Run the real transaction script through candidate and steady state."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ota-v2-committed-runtime-")
        self.root = Path(self.tmp.name)
        self.update = self.root / "update"
        self.staging = self.update / "staging"
        self.features = self.root / "features"
        self.proc = self.root / "proc"
        self.run_root = self.root / "run"
        self.parts = self.root / "parts"
        for path in (
            self.staging / "features/assistant", self.features / "assistant",
            self.proc / "self", self.proc / "net", self.proc / "123",
            self.run_root / "libreecho/features/assistant/root",
            self.run_root / "libreecho/features/assistant/runtime", self.parts,
        ):
            path.mkdir(parents=True, exist_ok=True)
        for feature_id in FEATURE_IDS[:-1]:
            (self.features / feature_id).mkdir(parents=True, exist_ok=True)

        self.base_payload = self.features / "assistant/payload.squashfs"
        self.base_manifest = self.features / "assistant/manifest.json"
        self.base_payload.write_bytes(b"base-payload")
        self.base_manifest.write_bytes(b"base-manifest")
        self.runtime_payload = b"runtime-payload"
        self.runtime_manifest = b"runtime-manifest"
        self.runtime_name = "libreecho-radar-puffin-0.13.11-assistant.runtime.squashfs"
        self.runtime_manifest_name = "libreecho-radar-puffin-0.13.11-assistant.runtime-manifest.json"
        (self.staging / "features/assistant" / self.runtime_name).write_bytes(self.runtime_payload)
        (self.staging / "features/assistant" / self.runtime_manifest_name).write_bytes(self.runtime_manifest)
        self.daemon = self.root / "new-daemon"
        self.daemon.write_bytes(b"new-runtime-daemon")

        self.boot = b"ANDROID!" + bytes(BOOT_SIZE - 8)
        (self.parts / "boot_b").write_bytes(self.boot)
        self.bcb = self.root / "bcb"
        self.bcb.write_text("selected_slot=b\nslot_b_success=0\nslot_a_success=1\n")
        (self.proc / "cmdline").write_bytes(b"androidboot.slot_suffix=_b\\0")
        self.mountinfo = self.proc / "self/mountinfo"
        (self.proc / "net/unix").write_text("Num RefCount Protocol Flags Type St Inode Path\n")
        self.mount_log = self.root / "mount.log"
        self.busybox = self.root / "busybox"
        self.busybox.write_text(
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = mount ]; then\n"
            f"  printf '%s\\n' \"$*\" >> {self.mount_log}\n"
            "  if [ \"${2:-}\" = --bind ]; then\n"
            "    source=$3; target=$4\n"
            f"    mkdir -p \"$(dirname \"$target\")\"; cp {self.daemon} \"$target\"\n"
            "    printf '41 25 0:41 / %s rw - none %s rw\\n' \"$target\" \"$source\" >> " + str(self.mountinfo) + "\n"
            "  else\n"
            "    source=; target=\n"
            "    for arg do source=$target; target=$arg; done\n"
            "    mkdir -p \"$target\"\n"
            f"    mkdir -p \"$target/usr/local/sbin\"; cp {self.daemon} \"$target/usr/local/sbin/libreecho-agentd\"\n"
            "    printf '40 25 0:40 / %s ro - squashfs %s ro\\n' \"$target\" \"$source\" >> " + str(self.mountinfo) + "\n"
            "  fi\n"
            "  exit 0\n"
            "fi\nexec /bin/busybox \"$@\"\n"
        )
        self.busybox.chmod(0o755)

        from feature_manifest import serialize_manifest
        record = feature("assistant", "runtime")
        record.update({
            "base_payload_sha256": hashlib.sha256(self.base_payload.read_bytes()).hexdigest(),
            "base_manifest_sha256": hashlib.sha256(self.base_manifest.read_bytes()).hexdigest(),
            "daemon_sha256": hashlib.sha256(self.daemon.read_bytes()).hexdigest(),
            "asset": self.runtime_name,
            "size": len(self.runtime_payload),
            "sha256": hashlib.sha256(self.runtime_payload).hexdigest(),
            "manifest_asset": self.runtime_manifest_name,
            "manifest_size": len(self.runtime_manifest),
            "manifest_sha256": hashlib.sha256(self.runtime_manifest).hexdigest(),
        })
        value = manifest([record])
        for feature_id in FEATURE_IDS[:-1]:
            payload = f"{feature_id}-base-payload".encode()
            metadata = f"{feature_id}-base-manifest".encode()
            (self.features / feature_id / "payload.squashfs").write_bytes(payload)
            (self.features / feature_id / "manifest.json").write_bytes(metadata)
            preserved = feature(feature_id, "preserve")
            preserved.update({
                "base_payload_sha256": hashlib.sha256(payload).hexdigest(),
                "base_manifest_sha256": hashlib.sha256(metadata).hexdigest(),
            })
            value["features"][FEATURE_IDS.index(feature_id)] = preserved
        value["feature_policy"] = "exclude"
        value["service_profile"] = "diagnostic"
        value["boot_sha256"] = hashlib.sha256(self.boot).hexdigest()
        manifest_path = self.staging / "manifest"
        manifest_path.write_bytes(serialize_manifest(value))
        self.signing_key = SigningKey.generate()
        self.public_key = self.root / "public-key.hex"
        self.verify = write_real_verifier(self.root, self.public_key, self.signing_key)
        (self.staging / "manifest.sig").write_text(
            self.signing_key.sign(manifest_path.read_bytes()).signature.hex() + "\n"
        )

        # Candidate service evidence for verify-running.
        self.proc.joinpath("123/exe").symlink_to(self.daemon)
        (self.run_root / "libreecho/assistant.sock").touch()
        (self.proc / "net/unix").write_text(
            "Num RefCount Protocol Flags Type St Inode Path\n"
            f"00000000: 00000002 00000000 00010000 0001 01 123 {self.run_root}/libreecho/assistant.sock\n"
        )
        self.var_run = self.root / "var/run"
        self.var_run.mkdir(parents=True)
        (self.var_run / "libreecho-agentd.pid").write_text("123\n")
        self.etc = self.root / "etc/libreecho"
        self.etc.mkdir(parents=True)
        (self.etc / "service-profile").write_text("diagnostic\n")
        (self.etc / "feature-policy").write_text("exclude\n")
        self.config = self.root / "config.json"
        self.config.write_text('{"integrations":17}\n')

        self.env = os.environ.copy()
        self.env.update({
            "BB": str(self.busybox),
            "LIBREECHO_TRANSACTION_ROOT": str(self.update),
            "LIBREECHO_TRANSACTION_STAGING": str(self.staging),
            "LIBREECHO_FEATURE_ROOT": str(self.features),
            "LIBREECHO_TRANSACTION_SLOT": "b",
            "LIBREECHO_BOOT_PARTITION_DIR": str(self.parts),
            "LIBREECHO_BCB_FILE": str(self.bcb),
            "LIBREECHO_PROC_ROOT": str(self.proc),
            "LIBREECHO_CMDLINE_FILE": str(self.proc / "cmdline"),
            "LIBREECHO_MOUNTINFO_FILE": str(self.mountinfo),
            "LIBREECHO_PROC_NET_UNIX": str(self.proc / "net/unix"),
            "LIBREECHO_VAR_RUN_ROOT": str(self.var_run),
            "LIBREECHO_ETC_ROOT": str(self.root / "etc"),
            "LIBREECHO_RUN_ROOT": str(self.run_root),
            "LIBREECHO_FEATURE_CONFIG": str(self.config),
            "LIBREECHO_VERIFY_BIN": str(self.verify),
            "LIBREECHO_PUBLIC_KEY": str(self.public_key),
            "LIBREECHO_TRANSACTION_TEST_MODE": "1",
        })

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def invoke(self, command: str) -> subprocess.CompletedProcess[str]:
        return run([str(transaction_fixture(self.root, self.env)), command], env=self.env)

    def stage_signed_manifest(self, transaction_id: str, action: str) -> None:
        from feature_manifest import parse_manifest, serialize_manifest

        value = parse_manifest((self.update / "committed-manifest").read_bytes())
        value["transaction_id"] = transaction_id
        for record in value["features"]:
            if record["feature_id"] == "assistant":
                record["action"] = action
                if action == "preserve":
                    for field in ("asset", "size", "sha256", "manifest_asset", "manifest_size", "manifest_sha256"):
                        record.pop(field, None)
                elif action == "replace":
                    prefix = "libreecho-radar-puffin-0.13.11-assistant"
                    record.update({
                        "asset": prefix + ".payload.squashfs",
                        "size": 1,
                        "sha256": "0" * 64,
                        "manifest_asset": prefix + ".manifest.json",
                        "manifest_size": 1,
                        "manifest_sha256": "1" * 64,
                    })
        self.staging.mkdir(parents=True, exist_ok=True)
        (self.staging / "features/assistant").mkdir(parents=True, exist_ok=True)
        manifest_path = self.staging / "manifest"
        manifest_path.write_bytes(serialize_manifest(value))
        (self.staging / "manifest.sig").write_text(
            self.signing_key.sign(manifest_path.read_bytes()).signature.hex() + "\n"
        )

    def commit_runtime_candidate(self) -> None:
        prepared = self.invoke("prepare-boot")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        activated = self.invoke("activate-mounts")
        self.assertEqual(activated.returncode, 0, activated.stderr)
        verified = self.invoke("verify-running")
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.bcb.write_text("selected_slot=b\nslot_b_success=1\nslot_a_success=1\n")
        committed = self.invoke("commit-after-confirm")
        self.assertEqual(committed.returncode, 0, committed.stderr)

    def test_runtime_then_two_preserves_remount_signed_inherited_capsule(self) -> None:
        self.commit_runtime_candidate()
        for generation in ("preserve-b", "preserve-c"):
            with self.subTest(generation=generation):
                self.stage_signed_manifest(generation, "preserve")
                self.assertEqual(self.invoke("prepare-boot").returncode, 0)
                self.mountinfo.write_text("")
                self.mount_log.write_text("")
                self.assertEqual(self.invoke("activate-mounts").returncode, 0)
                self.bcb.write_text("selected_slot=b\nslot_b_success=1\nslot_a_success=1\n")
                committed = self.invoke("commit-after-confirm")
                self.assertEqual(committed.returncode, 0, committed.stderr)
                self.mountinfo.write_text("")
                self.mount_log.write_text("")
                activated = self.invoke("activate-committed")
                self.assertEqual(activated.returncode, 0, activated.stderr)
                mounts = self.mount_log.read_text()
                self.assertIn(f"{self.features}/assistant/runtime.squashfs", mounts)
                self.assertIn("--bind", mounts)

    def test_preserve_conflict_and_missing_authority_fail_closed_before_mount(self) -> None:
        self.commit_runtime_candidate()
        self.stage_signed_manifest("preserve-conflict", "preserve")
        from feature_manifest import parse_manifest, serialize_manifest
        value = parse_manifest((self.staging / "manifest").read_bytes())
        record = next(item for item in value["features"] if item["feature_id"] == "assistant")
        record["daemon_sha256"] = "0" * 64
        manifest_path = self.staging / "manifest"
        manifest_path.write_bytes(serialize_manifest(value))
        (self.staging / "manifest.sig").write_text(
            self.signing_key.sign(manifest_path.read_bytes()).signature.hex() + "\n"
        )
        self.mountinfo.write_text("")
        self.mount_log.write_text("")
        self.assertEqual(self.invoke("prepare-boot").returncode, 0)
        rejected = self.invoke("activate-mounts")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.mount_log.read_text(), "")

        self.tmp.cleanup()
        self.setUp()
        self.commit_runtime_candidate()
        self.stage_signed_manifest("preserve-missing", "preserve")
        (self.update / "committed-runtime-assistant.sig").unlink()
        self.mountinfo.write_text("")
        self.mount_log.write_text("")
        self.assertEqual(self.invoke("prepare-boot").returncode, 0)
        rejected = self.invoke("activate-mounts")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.mount_log.read_text(), "")

    def test_tampered_inherited_runtime_authority_or_base_fails_before_mount(self) -> None:
        self.commit_runtime_candidate()
        authority = self.update / "committed-runtime-assistant.manifest"
        original = authority.read_bytes()
        authority.write_bytes(original.replace(b"feature_assistant_base_payload_sha256=", b"feature_assistant_base_payload_sha256=" + b"0"))
        self.mountinfo.write_text("")
        self.mount_log.write_text("")
        rejected = self.invoke("activate-committed")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.mount_log.read_text(), "")

        authority.write_bytes(original)
        self.base_payload.write_bytes(b"tampered-base")
        rejected = self.invoke("activate-committed")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.mount_log.read_text(), "")

    def test_replace_invalidates_inherited_runtime_capsule_and_authority(self) -> None:
        self.commit_runtime_candidate()
        self.stage_signed_manifest("replace-c", "replace")
        replacement = self.staging / "features/assistant" / "libreecho-radar-puffin-0.13.11-assistant.payload.squashfs"
        replacement_manifest = self.staging / "features/assistant" / "libreecho-radar-puffin-0.13.11-assistant.manifest.json"
        replacement.write_bytes(b"replacement-payload")
        replacement_manifest.write_bytes(b"replacement-manifest")
        from feature_manifest import parse_manifest, serialize_manifest
        value = parse_manifest((self.update / "committed-manifest").read_bytes())
        record = next(item for item in value["features"] if item["feature_id"] == "assistant")
        record["action"] = "replace"
        record.update({
            "base_payload_sha256": hashlib.sha256(self.base_payload.read_bytes()).hexdigest(),
            "base_manifest_sha256": hashlib.sha256(self.base_manifest.read_bytes()).hexdigest(),
            "asset": replacement.name,
            "size": replacement.stat().st_size,
            "sha256": hashlib.sha256(replacement.read_bytes()).hexdigest(),
            "manifest_asset": replacement_manifest.name,
            "manifest_size": replacement_manifest.stat().st_size,
            "manifest_sha256": hashlib.sha256(replacement_manifest.read_bytes()).hexdigest(),
        })
        manifest_path = self.staging / "manifest"
        manifest_path.write_bytes(serialize_manifest(value))
        (self.staging / "manifest.sig").write_text(
            self.signing_key.sign(manifest_path.read_bytes()).signature.hex() + "\n"
        )
        self.mountinfo.write_text("")
        self.mount_log.write_text("")
        self.commit_runtime_candidate()
        self.assertFalse((self.features / "assistant/runtime.squashfs").exists())
        self.assertFalse((self.features / "assistant/runtime-manifest.json").exists())
        self.assertFalse((self.update / "committed-runtime-assistant.manifest").exists())
        self.assertFalse((self.update / "committed-runtime-assistant.sig").exists())

    def test_runtime_commit_remounts_canonical_capsule_after_staging_cleanup(self) -> None:
        prepared = self.invoke("prepare-boot")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertEqual(self.invoke("activate-mounts").returncode, 0)
        self.assertEqual(self.invoke("verify-running").returncode, 0)

        self.bcb.write_text("selected_slot=b\nslot_b_success=1\nslot_a_success=1\n")
        committed = self.invoke("commit-after-confirm")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        self.assertEqual((self.features / "assistant/runtime.squashfs").read_bytes(), self.runtime_payload)
        self.assertTrue((self.update / "committed-manifest").is_file())
        self.assertTrue((self.update / "committed-manifest.sig").is_file())
        self.assertFalse(self.staging.exists())
        self.assertFalse((self.update / "pending").exists())

        self.mountinfo.write_text("")
        remounted = self.invoke("activate-committed")
        self.assertEqual(remounted.returncode, 0, remounted.stderr)
        self.assertIn(f"{self.features}/assistant/runtime.squashfs", self.mount_log.read_text())
        self.assertEqual(
            (self.run_root / "libreecho/features/assistant/root/usr/local/sbin/libreecho-agentd").read_bytes(),
            self.daemon.read_bytes(),
        )

    def test_installed_cleanup_boundaries_recover_and_activate_canonical_graph(self) -> None:
        for boundary in ("staging", "pending", "journal"):
            with self.subTest(boundary=boundary):
                prepared = self.invoke("prepare-boot")
                self.assertEqual(prepared.returncode, 0, prepared.stderr)
                self.assertEqual(self.invoke("activate-mounts").returncode, 0)
                self.assertEqual(self.invoke("verify-running").returncode, 0)
                self.bcb.write_text("selected_slot=b\nslot_b_success=1\nslot_a_success=1\n")

                fired = self.root / f"cleanup-fault-{boundary}.fired"
                faulty = self.root / f"busybox-cleanup-{boundary}"
                needle = {
                    "staging": "staging",
                    "pending": "pending",
                    "journal": "feature-commit",
                }[boundary]
                faulty.write_text(
                    "#!/bin/sh\n"
                    "if [ \"${1:-}\" = rm ]; then\n"
                    "  target=\n"
                    "  for arg do case \"$arg\" in *" + needle + ") target=$arg;; esac; done\n"
                    "  if [ -n \"$target\" ] && [ ! -e " + str(fired) + " ]; then\n"
                    "    /bin/busybox \"$@\" || exit $?\n"
                    "    : > " + str(fired) + "\n"
                    "    exit 97\n"
                    "  fi\n"
                    "fi\nexec /bin/busybox \"$@\"\n"
                )
                faulty.chmod(0o755)
                crashed = run(
                    [str(transaction_fixture(self.root, self.env | {"BB": str(faulty)})), "commit-after-confirm"], env=self.env | {"BB": str(faulty)}
                )
                self.assertNotEqual(crashed.returncode, 0, (boundary, crashed.stdout, crashed.stderr))
                recovered = run([str(transaction_fixture(self.root, self.env)), "recover-installed"], env=self.env)
                self.assertEqual(recovered.returncode, 0, (boundary, recovered.stdout, recovered.stderr))
                self.assertFalse(self.staging.exists())
                self.assertFalse((self.update / "pending").exists())
                self.assertFalse((self.update / "feature-commit").exists())
                self.mountinfo.write_text("")
                activated = self.invoke("activate-committed")
                self.assertEqual(activated.returncode, 0, (boundary, activated.stdout, activated.stderr))
                self.assertIn(f"{self.features}/assistant/runtime.squashfs", self.mount_log.read_text())
                self.tmp.cleanup()
                self.setUp()

    def test_fallback_cleans_only_when_bcb_proves_old_slot_is_confirmed(self) -> None:
        self.assertEqual(self.invoke("prepare-boot").returncode, 0)
        self.bcb.write_text("selected_slot=a\nslot_b_success=0\nslot_a_success=1\n")
        fallback = self.invoke("fallback")
        self.assertEqual(fallback.returncode, 0, fallback.stderr)
        self.assertFalse((self.update / "pending").exists())
        self.assertFalse((self.update / "feature-commit").exists())
        self.assertFalse(self.staging.exists())
        self.assertTrue((self.update / "rolled-back").is_file())
        self.assertEqual(self.base_payload.read_bytes(), b"base-payload")

        self.tmp.cleanup()
        self.setUp()
        self.assertEqual(self.invoke("prepare-boot").returncode, 0)
        self.bcb.write_text("selected_slot=a\nslot_b_success=0\nslot_a_success=0\n")
        ambiguous = self.invoke("fallback")
        self.assertNotEqual(ambiguous.returncode, 0)
        self.assertTrue((self.update / "pending").exists())
        self.assertTrue((self.update / "feature-commit").exists())
        self.assertTrue(self.staging.exists())
        self.assertEqual(self.base_payload.read_bytes(), b"base-payload")


class MultiFeatureRuntimeAuthorityTests(unittest.TestCase):
    """Keep independently signed runtime authorities independent across releases."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ota-v2-multi-runtime-")
        self.root = Path(self.tmp.name)
        self.update = self.root / "update"
        self.staging = self.update / "staging"
        self.features = self.root / "features"
        self.proc = self.root / "proc"
        self.run_root = self.root / "run"
        self.parts = self.root / "parts"
        self.etc = self.root / "etc"
        self.daemons = self.root / "daemons"
        for feature_id in FEATURE_IDS:
            (self.staging / "features" / feature_id).mkdir(parents=True)
            (self.features / feature_id).mkdir(parents=True)
        for path in (
            self.proc / "self", self.proc / "net", self.parts,
            self.run_root / "libreecho/features",
            self.daemons,
            self.etc / "libreecho",
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.base = {}
        self.daemon_hashes = {}
        for feature_id in FEATURE_IDS:
            payload = f"{feature_id}-base-payload".encode()
            metadata = f"{feature_id}-base-manifest".encode()
            daemon = f"{feature_id}-daemon".encode()
            (self.features / feature_id / "payload.squashfs").write_bytes(payload)
            (self.features / feature_id / "manifest.json").write_bytes(metadata)
            (self.daemons / feature_id).write_bytes(daemon)
            self.base[feature_id] = (payload, metadata)
            self.daemon_hashes[feature_id] = hashlib.sha256(daemon).hexdigest()
        self.boot = b"ANDROID!" + bytes(BOOT_SIZE - 8)
        self.boot_hash = hashlib.sha256(self.boot).hexdigest()
        (self.parts / "boot_b").write_bytes(self.boot)
        (self.parts / "boot_a").write_bytes(bytes(BOOT_SIZE))
        self.bcb = self.root / "bcb"
        self.bcb.write_text("selected_slot=b\nslot_b_success=0\nslot_a_success=1\n")
        (self.proc / "cmdline").write_bytes(b"androidboot.slot_suffix=_b\\0")
        (self.proc / "net/unix").write_text("Num RefCount Protocol Flags Type St Inode Path\\n")
        self.mountinfo = self.proc / "self/mountinfo"
        (self.etc / "libreecho/service-profile").write_text("diagnostic\n")
        (self.etc / "libreecho/feature-policy").write_text("exclude\n")
        self.mount_log = self.root / "mount.log"
        self.busybox = self.root / "busybox"
        mount_script = r'''#!/bin/sh
if [ "${1:-}" = mount ]; then
    printf '%s\n' "$*" >> "__MOUNT_LOG__"
    if [ "${2:-}" = --bind ]; then
        source=$3; target=$4
        mkdir -p "$(dirname "$target")"
        cp "$source" "$target"
        printf '60 25 0:60 / %s rw - none %s rw\n' "$target" "$source" >> "__MOUNTINFO__"
    else
        source=; target=
        for arg do
            case "$arg" in
                /*) [ -n "$target" ] && source=$target; target=$arg ;;
            esac
        done
        mkdir -p "$target"
        feature=${target#"__RUN_ROOT__/libreecho/features/"}
        feature=${feature%%/*}
        case "$feature" in
            assistant) daemon=usr/local/sbin/libreecho-agentd ;;
            tts) daemon=usr/local/sbin/libreecho-ttsd ;;
            airplay2) daemon=usr/local/sbin/libreecho-audio-engine ;;
            wakeword) daemon=usr/local/sbin/libreecho-waked ;;
            stt) daemon=usr/local/sbin/libreecho-sttd ;;
            *) daemon= ;;
        esac
        case "$target" in
            */root|*/runtime)
                mkdir -p "$target/$(dirname "$daemon")"
                cp "__DAEMONS__/$feature" "$target/$daemon"
                ;;
        esac
        printf '60 25 0:60 / %s ro - squashfs %s ro\n' "$target" "$source" >> "__MOUNTINFO__"
    fi
    exit 0
fi
exec /bin/busybox "$@"
'''
        self.busybox.write_text(
            mount_script.replace("__MOUNT_LOG__", str(self.mount_log))
            .replace("__MOUNTINFO__", str(self.mountinfo))
            .replace("__RUN_ROOT__", str(self.run_root))
            .replace("__DAEMONS__", str(self.daemons))
        )
        self.busybox.chmod(0o755)
        self.signing_key = SigningKey.generate()
        self.public_key = self.root / "public-key.hex"
        self.verify = write_real_verifier(self.root, self.public_key, self.signing_key)
        self.env = os.environ.copy()
        self.env.update({
            "BB": str(self.busybox),
            "LIBREECHO_TRANSACTION_ROOT": str(self.update),
            "LIBREECHO_TRANSACTION_STAGING": str(self.staging),
            "LIBREECHO_FEATURE_ROOT": str(self.features),
            "LIBREECHO_TRANSACTION_SLOT": "b",
            "LIBREECHO_BCB_FILE": str(self.bcb),
            "LIBREECHO_BOOT_PARTITION_DIR": str(self.parts),
            "LIBREECHO_PROC_ROOT": str(self.proc),
            "LIBREECHO_CMDLINE_FILE": str(self.proc / "cmdline"),
            "LIBREECHO_MOUNTINFO_FILE": str(self.mountinfo),
            "LIBREECHO_PROC_NET_UNIX": str(self.proc / "net/unix"),
            "LIBREECHO_RUN_ROOT": str(self.run_root),
            "LIBREECHO_ETC_ROOT": str(self.etc),
            "LIBREECHO_VERIFY_BIN": str(self.verify),
            "LIBREECHO_PUBLIC_KEY": str(self.public_key),
            "LIBREECHO_TRANSACTION_TEST_MODE": "1",
        })

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def record(self, feature_id: str, action: str, release: str) -> dict[str, object]:
        payload, metadata = self.base[feature_id]
        record = feature(feature_id, action)
        record.update({
            "release": release,
            "base_payload_sha256": hashlib.sha256(payload).hexdigest(),
            "base_manifest_sha256": hashlib.sha256(metadata).hexdigest(),
            "daemon_sha256": self.daemon_hashes[feature_id],
        })
        if action == "runtime":
            runtime_payload = f"{feature_id}-{release}-runtime".encode()
            runtime_metadata = f"{feature_id}-{release}-runtime-manifest".encode()
            prefix = f"libreecho-radar-puffin-{release}-{feature_id}"
            record.update({
                "asset": prefix + ".runtime.squashfs",
                "size": len(runtime_payload),
                "sha256": hashlib.sha256(runtime_payload).hexdigest(),
                "manifest_asset": prefix + ".runtime-manifest.json",
                "manifest_size": len(runtime_metadata),
                "manifest_sha256": hashlib.sha256(runtime_metadata).hexdigest(),
            })
        return record

    def stage(self, transaction_id: str, version: str, records: list[dict[str, object]]) -> None:
        from feature_manifest import serialize_manifest
        import shutil
        if self.staging.exists():
            shutil.rmtree(self.staging)
        for feature_id in FEATURE_IDS:
            (self.staging / "features" / feature_id).mkdir(parents=True)
        value = manifest(records)
        value["transaction_id"] = transaction_id
        value["version"] = version
        value["service_profile"] = "diagnostic"
        value["feature_policy"] = "exclude"
        value["boot_sha256"] = self.boot_hash
        manifest_path = self.staging / "manifest"
        manifest_path.write_bytes(serialize_manifest(value))
        (self.staging / "manifest.sig").write_text(
            self.signing_key.sign(manifest_path.read_bytes()).signature.hex() + "\n"
        )
        for record in value["features"]:
            if record["action"] != "runtime":
                continue
            feature_id = str(record["feature_id"])
            payload = f"{feature_id}-{record['release']}-runtime".encode()
            metadata = f"{feature_id}-{record['release']}-runtime-manifest".encode()
            source = self.staging / "features" / feature_id
            (source / str(record["asset"])).write_bytes(payload)
            (source / str(record["manifest_asset"])).write_bytes(metadata)

    def invoke(self, command: str) -> subprocess.CompletedProcess[str]:
        return run([str(transaction_fixture(self.root, self.env)), command], env=self.env)

    def commit_generation(self, transaction_id: str, version: str, records: list[dict[str, object]]) -> None:
        self.stage(transaction_id, version, records)
        prepared = self.invoke("prepare-boot")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.mountinfo.write_text("")
        self.mount_log.write_text("")
        activated = self.invoke("activate-mounts")
        self.assertEqual(activated.returncode, 0, activated.stderr)
        verified = self.invoke("verify-running")
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.bcb.write_text("selected_slot=b\nslot_b_success=1\nslot_a_success=1\n")
        committed = self.invoke("commit-after-confirm")
        self.assertEqual(committed.returncode, 0, committed.stderr)

    def generation_records(self, assistant_action: str, tts_action: str, release: str) -> list[dict[str, object]]:
        return [
            self.record("assistant", assistant_action, "0.13.11" if assistant_action == "runtime" else release),
            self.record("tts", tts_action, "0.13.12" if tts_action == "runtime" else release),
            *[self.record(feature_id, "preserve", release) for feature_id in FEATURE_IDS if feature_id not in {"assistant", "tts"}],
        ]

    def establish_a_b(self) -> None:
        self.commit_generation("release-a", "0.13.11", self.generation_records("runtime", "preserve", "0.13.11"))
        self.commit_generation("release-b", "0.13.12", self.generation_records("preserve", "runtime", "0.13.12"))

    def test_release_a_assistant_and_release_b_tts_both_preserve_c_activate_independently(self) -> None:
        self.establish_a_b()
        self.assertIn("feature_assistant_release=0.13.11", (self.update / "committed-runtime-assistant.manifest").read_text())
        self.assertIn("feature_tts_release=0.13.12", (self.update / "committed-runtime-tts.manifest").read_text())
        self.stage("preserve-c", "0.13.13", self.generation_records("preserve", "preserve", "0.13.13"))
        prepared = self.invoke("prepare-boot")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.mountinfo.write_text("")
        self.mount_log.write_text("")
        candidate = self.invoke("activate-mounts")
        self.assertEqual(candidate.returncode, 0, candidate.stderr)
        mounts = self.mount_log.read_text()
        self.assertIn(str(self.features / "assistant/payload.squashfs"), mounts)
        self.assertIn(str(self.features / "assistant/runtime.squashfs"), mounts)
        self.assertIn(str(self.features / "tts/payload.squashfs"), mounts)
        self.assertIn(str(self.features / "tts/runtime.squashfs"), mounts)

        self.bcb.write_text("selected_slot=b\nslot_b_success=1\nslot_a_success=1\n")
        committed = self.invoke("commit-after-confirm")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        self.mountinfo.write_text("")
        self.mount_log.write_text("")
        normal = self.invoke("activate-committed")
        self.assertEqual(normal.returncode, 0, normal.stderr)
        normal_mounts = self.mount_log.read_text()
        self.assertIn(str(self.features / "assistant/runtime.squashfs"), normal_mounts)
        self.assertIn(str(self.features / "tts/runtime.squashfs"), normal_mounts)
        self.assertNotEqual(
            (self.update / "committed-runtime-assistant.manifest").read_bytes(),
            (self.update / "committed-runtime-tts.manifest").read_bytes(),
        )

    def test_tts_authority_tamper_is_rejected_before_any_candidate_mount(self) -> None:
        self.establish_a_b()
        self.stage("preserve-c-tamper", "0.13.13", self.generation_records("preserve", "preserve", "0.13.13"))
        self.assertEqual(self.invoke("prepare-boot").returncode, 0)
        authority = self.update / "committed-runtime-tts.manifest"
        tampered = authority.read_bytes().replace(
            (b"feature_tts_daemon_sha256=" + self.daemon_hashes["tts"].encode()),
            b"feature_tts_daemon_sha256=" + b"0" * 64,
        )
        authority.write_bytes(tampered)
        (self.update / "committed-runtime-tts.sig").write_text(
            self.signing_key.sign(tampered).signature.hex() + "\n"
        )
        self.mountinfo.write_text("")
        self.mount_log.write_text("")
        rejected = self.invoke("activate-mounts")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.mount_log.read_text(), "")


class CleanupCompatibilityTests(unittest.TestCase):
    def test_v1_pending_keeps_canonical_features_but_preserves_old_staging_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ota-v1-cleanup-") as directory:
            root = Path(directory)
            data = root / "data"
            feature = data / "libreecho/features/assistant"
            staging = data / "libreecho/update/staging"
            feature.mkdir(parents=True)
            staging.mkdir(parents=True)
            (feature / "payload.squashfs").write_bytes(b"canonical")
            (feature / "manifest.json").write_bytes(b"canonical-manifest")
            (data / "libreecho/update/pending").write_text("schema=1\nslot=b\n")
            (staging / "old-v1-residue").write_bytes(b"residue")
            result = run([
                "/bin/sh", str(TOOLS / "initramfs/libreecho-data-cleanup"),
            ], env={**os.environ, "LIBREECHO_DATA_TEST_MODE": "1", "DATA_ROOT": str(data)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((data / "libreecho/update/pending").exists())
            self.assertFalse((staging / "old-v1-residue").exists())
            self.assertEqual((feature / "payload.squashfs").read_bytes(), b"canonical")
            self.assertEqual((feature / "manifest.json").read_bytes(), b"canonical-manifest")


if __name__ == "__main__":
    unittest.main()

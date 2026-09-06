#!/usr/bin/env python3
"""Verify a deterministic, deliberately narrow runtime replacement capsule.

The capsule format is SquashFS 4 with lz4 compression.  Verification requires
both ``unsquashfs`` and a manifest that describes the exact replacement file;
there is no tar or other format fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


ALLOWLIST = {
    "airplay2": "usr/local/sbin/libreecho-audio-engine",
    "tts": "usr/local/sbin/libreecho-ttsd",
    "wakeword": "usr/local/sbin/libreecho-waked",
    "stt": "usr/local/sbin/libreecho-sttd",
    "assistant": "usr/local/sbin/libreecho-agentd",
}
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
COMMIT40 = re.compile(r"[0-9a-f]{40}\Z")
FEATURE_ID = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]*\Z")
MODE = re.compile(r"[0-7]{4}\Z")
MAX_ALLOWED_BYTES = 64 * 1024 * 1024
# SquashFS v4 stores xattr_id_table_start at byte 56; the all-ones sentinel
# is the only accepted value because runtime capsules must carry no xattrs.
SQUASHFS_MIN_SUPERBLOCK = 64
SQUASHFS_MAGIC = b"hsqs"
SQUASHFS_XATTR_TABLE_OFFSET = 56
SQUASHFS_INVALID_BLOCK = (1 << 64) - 1


class CapsuleError(Exception):
    """A user-facing, bounded validation error."""


def error(message: str) -> CapsuleError:
    return CapsuleError(message.replace("\n", " ")[:240])


def regular(path: Path, label: str) -> None:
    """Require a regular file and reject symlinked parent components."""
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parent.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except OSError as exc:
            raise error(f"{label} parent is missing or inaccessible") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise error(f"{label} parent contains a symlink or non-directory")
    try:
        info = path.lstat()
    except OSError as exc:
        raise error(f"{label} is missing or inaccessible") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise error(f"{label} must be a regular non-symlink file")
    if stat.S_IMODE(info.st_mode) & 0o7000:
        raise error(f"{label} has unsafe special permission bits")


def digest(path: Path) -> tuple[str, int]:
    value = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            value.update(chunk)
            size += len(chunk)
    return value.hexdigest(), size


def safe_relative(value: str) -> str:
    if not value or "\\" in value or value.startswith("/"):
        raise error("archive member path is unsafe")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise error("archive member path is unsafe")
    return value


def strict_json(data: bytes, label: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate key: {key}")
            value[key] = item
        return value

    try:
        text = data.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise error(f"{label} is malformed") from exc


def canonical_json(data: Any) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()


def integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error(f"{label} must be an integer")
    return value


def validate_max_bytes(value: Any, label: str = "max-bytes") -> int:
    limit = integer(value, label)
    if limit <= 0 or limit > MAX_ALLOWED_BYTES:
        raise error(f"{label} must be between 1 and {MAX_ALLOWED_BYTES}")
    return limit


def validate_hex(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise error(f"{label} is malformed")
    return value


def validate_mode(value: Any, label: str) -> str:
    if not isinstance(value, str) or MODE.fullmatch(value) is None:
        raise error(f"{label} is malformed")
    if int(value, 8) & 0o7000:
        raise error(f"{label} has unsafe special permission bits")
    return value


def validate_contract(
    feature_id: str,
    component: str,
    component_version: str,
    build_identity: str,
    service_dependencies: list[str],
    compatibility: dict[str, Any],
) -> tuple[str, str, str, list[str], dict[str, Any]]:
    expected_component = ALLOWLIST[feature_id].rsplit("/", 1)[-1]
    if component != expected_component or IDENTIFIER.fullmatch(component) is None:
        raise error("component does not match the compiled allowlist")
    if not isinstance(component_version, str) or IDENTIFIER.fullmatch(component_version) is None:
        raise error("component version is malformed")
    if not isinstance(build_identity, str) or IDENTIFIER.fullmatch(build_identity) is None:
        raise error("build identity is malformed")
    if (
        not isinstance(service_dependencies, list)
        or not service_dependencies
        or any(not isinstance(item, str) or IDENTIFIER.fullmatch(item) is None for item in service_dependencies)
        or len(set(service_dependencies)) != len(service_dependencies)
    ):
        raise error("service dependencies are malformed")
    if not isinstance(compatibility, dict) or set(compatibility) != {"abi", "model", "mounts", "dependencies"}:
        raise error("compatibility constraints are malformed")
    for field in ("abi", "model"):
        if not isinstance(compatibility[field], str) or IDENTIFIER.fullmatch(compatibility[field]) is None:
            raise error(f"compatibility {field} is malformed")
    mounts = compatibility["mounts"]
    if (
        not isinstance(mounts, list)
        or not mounts
        or any(
            not isinstance(item, str)
            or not item.startswith("/")
            or "//" in item
            or any(part in ("", ".", "..") for part in item.split("/")[1:])
            for item in mounts
        )
        or len(set(mounts)) != len(mounts)
    ):
        raise error("compatibility mounts are malformed")
    dependencies = compatibility["dependencies"]
    if dependencies != service_dependencies:
        raise error("compatibility dependencies do not match service dependencies")
    return component, component_version, build_identity, service_dependencies, compatibility


def validate_inputs(feature_id: str, product_release: str, source_commit: str) -> None:
    if feature_id not in ALLOWLIST or FEATURE_ID.fullmatch(feature_id) is None:
        raise error("feature id is unsupported")
    if not isinstance(product_release, str) or not product_release or "\n" in product_release or "\r" in product_release:
        raise error("product release is malformed")
    if COMMIT40.fullmatch(source_commit) is None:
        raise error("source commit is malformed")


def validate_base_manifest(data: Any, feature_id: str, payload: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(data, dict):
        raise error("base manifest must be an object")
    if set(data) != {"schema_version", "feature_id", "format", "payload", "files"}:
        raise error("base manifest has unknown or missing fields")
    if integer(data["schema_version"], "base schema_version") != 1:
        raise error("base schema_version is unsupported")
    if data["feature_id"] != feature_id:
        raise error("base feature id does not match")
    if data["format"] != "squashfs-lz4":
        raise error("base manifest format is unsupported")
    record = data["payload"]
    if not isinstance(record, dict) or set(record) != {"filename", "sha256", "size"}:
        raise error("base payload record is malformed")
    if record["filename"] != payload.name:
        raise error("base payload filename does not match")
    validate_hex(record["sha256"], HEX64, "base payload hash")
    if integer(record["size"], "base payload size") < 0:
        raise error("base payload size is invalid")
    files = data["files"]
    if not isinstance(files, dict) or not files:
        raise error("base files record is malformed")
    for name, item in files.items():
        if not isinstance(name, str):
            raise error("base file name is malformed")
        safe_relative(name)
        if not isinstance(item, dict) or set(item) != {"sha256", "size", "mode"}:
            raise error("base file record is malformed")
        validate_hex(item["sha256"], HEX64, "base file hash")
        if integer(item["size"], "base file size") < 0:
            raise error("base file size is invalid")
        validate_mode(item["mode"], "base file mode")
    actual_hash, actual_size = digest(payload)
    if actual_hash != record["sha256"] or actual_size != record["size"]:
        raise error("base payload does not match its manifest")
    return data, files


def perms_to_mode(perms: str) -> str:
    mode = 0
    for index, bit in enumerate((0o400, 0o200, 0o100, 0o040, 0o020, 0o010, 0o004, 0o002, 0o001)):
        if perms[index + 1] != "-":
            mode |= bit
    return f"{mode:04o}"


def require_lz4(payload: Path) -> None:
    try:
        with payload.open("rb") as stream:
            superblock = stream.read(SQUASHFS_MIN_SUPERBLOCK)
    except OSError as exc:
        raise error("unable to inspect SquashFS superblock") from exc
    if (
        len(superblock) != SQUASHFS_MIN_SUPERBLOCK
        or superblock[:4] != SQUASHFS_MAGIC
    ):
        raise error("unable to inspect SquashFS superblock")
    xattr_table = int.from_bytes(
        superblock[
            SQUASHFS_XATTR_TABLE_OFFSET:SQUASHFS_XATTR_TABLE_OFFSET + 8
        ],
        "little",
    )
    if xattr_table != SQUASHFS_INVALID_BLOCK:
        raise error("payload contains extended attributes")

    unsquashfs = shutil.which("unsquashfs")
    if unsquashfs is None:
        raise error("unsquashfs is required to verify squashfs-lz4 capsules")
    try:
        result = subprocess.run(
            [unsquashfs, "-s", str(payload)],
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise error("unable to inspect SquashFS superblock") from exc
    if result.returncode != 0:
        raise error("unable to inspect SquashFS superblock")
    compressions = [
        fields[1]
        for line in result.stdout.splitlines()
        if len(fields := line.split()) == 2 and fields[0] == "Compression"
    ]
    if compressions != ["lz4"]:
        raise error("payload compression is not lz4")


def list_members(payload: Path) -> dict[str, tuple[str, int, str]]:
    require_lz4(payload)
    unsquashfs = shutil.which("unsquashfs")
    if unsquashfs is None:
        raise error("unsquashfs is required to verify squashfs-lz4 capsules")
    environment = os.environ.copy()
    environment["TZ"] = "UTC"
    try:
        result = subprocess.run(
            [unsquashfs, "-lln", "-full-precision", "-UTC", "-no-progress", str(payload)],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise error("unable to inspect SquashFS payload") from exc
    if result.returncode != 0:
        raise error("unable to inspect SquashFS payload")

    members: dict[str, tuple[str, int, str]] = {}
    permission = re.compile(r"^[d-][rwx-]{9}$")
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split(maxsplit=5)
        if len(fields) != 6 or not permission.fullmatch(fields[0]):
            raise error("payload listing is malformed")
        perms, owner, size, date, time, member_name = fields
        if owner != "0/0" or date != "1970-01-01" or time != "00:00:00":
            raise error("payload metadata is not normalized")
        if perms[0] not in "d-":
            raise error("payload contains a symlink or unsupported member type")
        root_name = "squashfs-root"
        if member_name == root_name:
            name = "/"
        elif member_name.startswith(root_name + "/"):
            name = member_name[len(root_name) + 1:]
            safe_relative(name)
        else:
            raise error("payload has an unexpected root name")
        try:
            member_size = int(size)
        except ValueError as exc:
            raise error("payload member size is malformed") from exc
        if member_size < 0 or name in members:
            raise error("payload member list is invalid or duplicated")
        members[name] = (perms[0], member_size, perms_to_mode(perms))
    if "/" not in members:
        raise error("payload has no root directory")
    return members


def cat_hash(payload: Path, member: str, expected_size: int) -> tuple[str, int]:
    unsquashfs = shutil.which("unsquashfs")
    if unsquashfs is None:
        raise error("unsquashfs is required to verify squashfs-lz4 capsules")
    environment = os.environ.copy()
    environment["TZ"] = "UTC"
    try:
        process = subprocess.Popen(
            [unsquashfs, "-cat", "-no-progress", "-no-wildcards", str(payload), member],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
    except OSError as exc:
        raise error("unable to read SquashFS member") from exc
    assert process.stdout is not None
    value = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = process.stdout.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > expected_size:
                process.kill()
                process.wait(timeout=10)
                raise error("SquashFS member is larger than its manifest")
            value.update(chunk)
        if process.wait(timeout=10) != 0:
            raise error("unable to read SquashFS member")
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait(timeout=10)
        raise error("timed out reading SquashFS member") from exc
    return value.hexdigest(), size


def expected_directories(files: set[str]) -> set[str]:
    directories = {"/"}
    for name in files:
        parts = name.split("/")
        directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    return directories


def verify_capsule(
    feature_id: str,
    base_payload: Path,
    base_manifest: Path,
    product_release: str,
    source_commit: str,
    component: str,
    component_version: str,
    build_identity: str,
    service_dependencies: list[str],
    compatibility: dict[str, Any],
    capsule: Path,
    manifest_path: Path,
    max_bytes: int,
) -> dict[str, Any]:
    validate_inputs(feature_id, product_release, source_commit)
    max_bytes = validate_max_bytes(max_bytes)
    validate_contract(feature_id, component, component_version, build_identity, service_dependencies, compatibility)
    for path, label in (
        (base_payload, "base payload"),
        (base_manifest, "base manifest"),
        (capsule, "capsule"),
        (manifest_path, "capsule manifest"),
    ):
        regular(path, label)
    if capsule.absolute() == manifest_path.absolute():
        raise error("capsule and capsule manifest must be different paths")

    base_manifest_bytes = base_manifest.read_bytes()
    base_data = strict_json(base_manifest_bytes, "base manifest")
    if base_manifest_bytes != canonical_json(base_data):
        raise error("base manifest is not canonical")
    base_data, base_files = validate_base_manifest(base_data, feature_id, base_payload)
    base_payload_record = base_data["payload"]
    base_manifest_hash = hashlib.sha256(base_manifest_bytes).hexdigest()

    manifest_bytes = manifest_path.read_bytes()
    manifest = strict_json(manifest_bytes, "capsule manifest")
    if manifest_bytes != canonical_json(manifest):
        raise error("capsule manifest is not canonical")
    if not isinstance(manifest, dict):
        raise error("capsule manifest must be an object")
    expected_fields = {
        "base_manifest_sha256", "base_payload_sha256", "base_payload_size", "build_identity",
        "component", "component_version", "feature_id", "files", "format", "kind", "payload",
        "product_release", "schema_version", "service_dependencies", "source_commit", "compatibility",
        "max_bytes",
    }
    if set(manifest) != expected_fields:
        raise error("capsule manifest has unknown or missing fields")
    if integer(manifest["schema_version"], "schema_version") != 1:
        raise error("schema_version is unsupported")
    if manifest["kind"] != "runtime-capsule":
        raise error("manifest kind is unsupported")
    if manifest["format"] != "squashfs-lz4":
        raise error("manifest format is unsupported")
    if manifest["feature_id"] != feature_id:
        raise error("manifest feature id does not match")
    if manifest["product_release"] != product_release:
        raise error("manifest product release does not match")
    if manifest["source_commit"] != source_commit:
        raise error("manifest source commit does not match")
    if manifest["component"] != component:
        raise error("manifest component does not match")
    if manifest["component_version"] != component_version:
        raise error("manifest component version does not match")
    if manifest["build_identity"] != build_identity:
        raise error("manifest build identity does not match")
    if manifest["service_dependencies"] != service_dependencies:
        raise error("manifest service dependencies do not match")
    if manifest["compatibility"] != compatibility:
        raise error("manifest compatibility constraints do not match")
    manifest_max_bytes = validate_max_bytes(manifest["max_bytes"], "manifest max-bytes")
    if manifest_max_bytes != max_bytes:
        raise error("manifest max-bytes does not match trusted cap")
    validate_contract(
        feature_id,
        manifest["component"],
        manifest["component_version"],
        manifest["build_identity"],
        manifest["service_dependencies"],
        manifest["compatibility"],
    )
    if manifest["base_manifest_sha256"] != base_manifest_hash:
        raise error("base manifest hash does not match")
    validate_hex(manifest["base_manifest_sha256"], HEX64, "base manifest hash")
    validate_hex(manifest["base_payload_sha256"], HEX64, "base payload hash")
    if manifest["base_payload_sha256"] != base_payload_record["sha256"]:
        raise error("base payload hash does not match base manifest")
    if integer(manifest["base_payload_size"], "base payload size") < 0:
        raise error("base payload size is invalid")
    if manifest["base_payload_size"] != base_payload_record["size"]:
        raise error("base payload size does not match base manifest")

    payload = manifest["payload"]
    if not isinstance(payload, dict) or set(payload) != {"filename", "sha256", "size"}:
        raise error("payload record is malformed")
    if payload["filename"] != capsule.name:
        raise error("payload filename does not match")
    validate_hex(payload["sha256"], HEX64, "payload hash")
    if integer(payload["size"], "payload size") < 0:
        raise error("payload size is invalid")
    if payload["size"] > max_bytes or capsule.stat().st_size > max_bytes:
        raise error("capsule payload exceeds max-bytes")
    actual_hash, actual_size = digest(capsule)
    if actual_hash != payload["sha256"] or actual_size != payload["size"]:
        raise error("capsule payload does not match its manifest")

    target = ALLOWLIST[feature_id]
    files = manifest["files"]
    if not isinstance(files, dict) or set(files) != {target}:
        raise error("manifest files do not match the compiled allowlist")
    record = files[target]
    if not isinstance(record, dict) or set(record) != {"base_sha256", "mode", "sha256", "size"}:
        raise error("manifest file record is malformed")
    base_record = base_files.get(target)
    if base_record is None:
        raise error("allowlisted target is absent from the base manifest")
    base_members = list_members(base_payload)
    base_member = base_members.get(target)
    if (
        base_member is None
        or base_member[0] != "-"
        or base_member[1] != base_record["size"]
        or base_member[2] != base_record["mode"]
    ):
        raise error("allowlisted target metadata does not match the base manifest")
    base_hash, base_size = cat_hash(base_payload, target, base_record["size"])
    if base_hash != base_record["sha256"] or base_size != base_record["size"]:
        raise error("allowlisted target content does not match the base manifest")
    validate_hex(record["base_sha256"], HEX64, "base target hash")
    if record["base_sha256"] != base_record["sha256"]:
        raise error("base target hash does not match base manifest")
    validate_mode(record["mode"], "replacement mode")
    if record["mode"] != "0755":
        raise error("replacement mode is not 0755")
    validate_hex(record["sha256"], HEX64, "replacement hash")
    if integer(record["size"], "replacement size") < 0:
        raise error("replacement size is invalid")
    if record["size"] > max_bytes:
        raise error("replacement size exceeds max-bytes")

    members = list_members(capsule)
    expected = expected_directories({target}) | {target}
    if set(members) != expected:
        raise error("capsule contains unexpected or missing members")
    for name in expected_directories({target}):
        kind, _, mode = members[name]
        if kind != "d" or mode != "0755":
            raise error("capsule directory metadata is not normalized")
    kind, member_size, mode = members[target]
    if kind != "-" or mode != record["mode"] or member_size != record["size"]:
        raise error("replacement member metadata does not match its manifest")
    actual_hash, actual_size = cat_hash(capsule, target, record["size"])
    if actual_hash != record["sha256"] or actual_size != record["size"]:
        raise error("replacement member does not match its manifest")
    return manifest


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feature-id", required=True)
    parser.add_argument("--base-payload", type=Path, required=True)
    parser.add_argument("--base-manifest", type=Path, required=True)
    parser.add_argument("--product-release", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--component", required=True)
    parser.add_argument("--component-version", required=True)
    parser.add_argument("--build-identity", required=True)
    parser.add_argument("--service-dependency", action="append", required=True)
    parser.add_argument("--compatibility", required=True, help="canonical JSON object of ABI/model/mount/dependency constraints")
    parser.add_argument("--max-bytes", type=int, required=True, help="trusted positive capsule size cap in bytes")
    parser.add_argument("--capsule", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        compatibility = strict_json(args.compatibility.encode(), "compatibility")
        if not isinstance(compatibility, dict):
            raise error("compatibility constraints must be an object")
        verify_capsule(
            args.feature_id,
            args.base_payload,
            args.base_manifest,
            args.product_release,
            args.source_commit,
            args.component,
            args.component_version,
            args.build_identity,
            args.service_dependency,
            compatibility,
            args.capsule,
            args.manifest,
            args.max_bytes,
        )
    except (CapsuleError, OSError, ValueError) as exc:
        print(f"ERROR: {str(exc)[:240]}", file=sys.stderr)
        return 1
    print(f"verified={args.capsule}")
    print(f"manifest={args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

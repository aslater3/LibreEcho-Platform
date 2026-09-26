#!/usr/bin/env python3
"""Build a deterministic, deliberately narrow runtime replacement capsule."""

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
import tempfile
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


class CapsuleError(Exception):
    """A user-facing, bounded validation error."""


def error(message: str) -> CapsuleError:
    return CapsuleError(message.replace("\n", " ")[:240])


def regular(path: Path, label: str) -> None:
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


def ensure_parent(path: Path, label: str) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parent.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir()
            info = current.lstat()
        except OSError as exc:
            raise error(f"{label} parent is inaccessible") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise error(f"{label} parent contains a symlink or non-directory")


def absent(path: Path, label: str) -> None:
    if os.path.lexists(path):
        raise error(f"{label} already exists")


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
        raise error("replacement target is unsafe")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise error("replacement target is unsafe")
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


def validate_base_manifest(data: Any, feature_id: str, payload: Path) -> tuple[dict[str, Any], bytes]:
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


def require_lz4(payload: Path) -> None:
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


def list_members(payload: Path) -> dict[str, tuple[str, int, str, str, str]]:
    require_lz4(payload)
    unsquashfs = shutil.which("unsquashfs")
    if unsquashfs is None:
        raise error("unsquashfs is required to verify squashfs-lz4 capsules")
    environment = os.environ.copy()
    environment["TZ"] = "UTC"
    try:
        result = subprocess.run(
            [unsquashfs, "-lln", "-full-precision", "-UTC", "-no-progress", str(payload)],
            text=True, capture_output=True, check=False, env=environment, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise error("unable to inspect SquashFS payload") from exc
    if result.returncode != 0:
        raise error("unable to inspect SquashFS payload")
    members: dict[str, tuple[str, int, str, str, str]] = {}
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
        members[name] = (perms[0], member_size, perms_to_mode(perms), date, time)
    if "/" not in members:
        raise error("payload has no root directory")
    return members


def perms_to_mode(perms: str) -> str:
    mode = 0
    for index, bit in enumerate((0o400, 0o200, 0o100, 0o040, 0o020, 0o010, 0o004, 0o002, 0o001)):
        if perms[index + 1] != "-":
            mode |= bit
    return f"{mode:04o}"


def cat_hash(payload: Path, member: str, expected_size: int | None = None) -> tuple[str, int]:
    unsquashfs = shutil.which("unsquashfs")
    if unsquashfs is None:
        raise error("unsquashfs is required to verify squashfs-lz4 capsules")
    environment = os.environ.copy()
    environment["TZ"] = "UTC"
    try:
        process = subprocess.Popen(
            [unsquashfs, "-cat", "-no-progress", "-no-wildcards", str(payload), member],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
        )
    except OSError as exc:
        raise error("unable to read SquashFS member") from exc
    assert process.stdout is not None
    value = hashlib.sha256()
    size = 0
    while True:
        chunk = process.stdout.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if expected_size is not None and size > expected_size:
            process.kill()
            process.wait(timeout=10)
            raise error("SquashFS member is larger than its manifest")
        value.update(chunk)
    stderr = process.stderr.read() if process.stderr is not None else b""
    if process.wait(timeout=10) != 0:
        del stderr
        raise error("unable to read SquashFS member")
    return value.hexdigest(), size


def validate_inputs(feature_id: str, product_release: str, source_commit: str) -> None:
    if feature_id not in ALLOWLIST or FEATURE_ID.fullmatch(feature_id) is None:
        raise error("feature id is unsupported")
    if not isinstance(product_release, str) or not product_release or "\n" in product_release or "\r" in product_release:
        raise error("product release is malformed")
    if COMMIT40.fullmatch(source_commit) is None:
        raise error("source commit is malformed")


def build_capsule(
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
    replacements: list[str],
    output: Path,
    manifest_path: Path,
    max_bytes: int,
) -> dict[str, Any]:
    validate_inputs(feature_id, product_release, source_commit)
    max_bytes = validate_max_bytes(max_bytes)
    validate_contract(feature_id, component, component_version, build_identity, service_dependencies, compatibility)
    regular(base_payload, "base payload")
    regular(base_manifest, "base manifest")
    ensure_parent(output, "capsule")
    ensure_parent(manifest_path, "capsule manifest")
    if output.absolute() == manifest_path.absolute():
        raise error("capsule and capsule manifest must be different paths")
    absent(output, "capsule")
    absent(manifest_path, "capsule manifest")
    base_data, base_files = validate_base_manifest(
        strict_json(base_manifest.read_bytes(), "base manifest"), feature_id, base_payload,
    )
    target = ALLOWLIST[feature_id]
    parsed: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for mapping in replacements:
        replacement_target, separator, source_name = mapping.partition("=")
        if not separator or replacement_target in seen:
            raise error("replacement mapping is malformed or duplicated")
        replacement_target = safe_relative(replacement_target)
        if replacement_target != target:
            raise error("replacement target is not in the compiled allowlist")
        source = Path(source_name)
        regular(source, "replacement source")
        if source.stat().st_size > max_bytes:
            raise error("replacement source exceeds max-bytes")
        seen.add(replacement_target)
        parsed.append((replacement_target, source))
    if not parsed:
        raise error("at least one replacement is required")
    base_members = list_members(base_payload)
    for replacement_target, _ in parsed:
        record = base_files.get(replacement_target)
        member = base_members.get(replacement_target)
        if record is None or member is None or member[0] != "-" or member[2] != record["mode"]:
            raise error("replacement target is not a matching regular file in the base payload")
        old_hash, old_size = cat_hash(base_payload, replacement_target, record["size"])
        if old_hash != record["sha256"] or old_size != record["size"]:
            raise error("base target does not match its manifest")

    output_written = False
    manifest_written = False
    staged_output: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="runtime-capsule-", dir=str(output.parent)) as temporary:
            root = Path(temporary)
            for replacement_target, source in parsed:
                destination = root / replacement_target
                destination.parent.mkdir(parents=True, exist_ok=True)
                for directory in [root, *destination.parent.parents]:
                    if directory.is_relative_to(root):
                        directory.chmod(0o755)
                destination.write_bytes(source.read_bytes())
                destination.chmod(0o755)
            for directory in sorted((path for path in root.rglob("*") if path.is_dir()), key=lambda item: len(item.parts)):
                directory.chmod(0o755)
            environment = os.environ.copy()
            environment.pop("SOURCE_DATE_EPOCH", None)
            environment["TZ"] = "UTC"
            mksquashfs = shutil.which("mksquashfs")
            if mksquashfs is None:
                raise error("mksquashfs is required for squashfs-lz4 capsules; no archive fallback is used")
            staged_output = root.with_suffix(".squashfs")
            command = [
                mksquashfs, str(root), str(staged_output), "-noappend", "-comp", "lz4",
                "-all-root", "-force-uid", "0", "-force-gid", "0", "-no-xattrs",
                "-mkfs-time", "0", "-all-time", "0", "-root-mode", "0755",
                "-no-duplicates", "-no-hardlinks", "-processors", "1", "-no-progress",
            ]
            try:
                result = subprocess.run(command, text=True, capture_output=True, check=False, env=environment, timeout=120)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise error("mksquashfs failed") from exc
            if result.returncode != 0:
                detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
                raise error(f"mksquashfs failed: {detail}")
            if staged_output.stat().st_size > max_bytes:
                raise error("capsule payload exceeds max-bytes")
            os.replace(staged_output, output)
        output_written = True
        payload_hash, payload_size = digest(output)
        files: dict[str, dict[str, Any]] = {}
        for replacement_target, source in parsed:
            source_hash, source_size = digest(source)
            base_record = base_files[replacement_target]
            files[replacement_target] = {
                "base_sha256": base_record["sha256"],
                "mode": "0755",
                "sha256": source_hash,
                "size": source_size,
            }
        base_payload_record = base_data["payload"]
        runtime_manifest: dict[str, Any] = {
            "base_manifest_sha256": hashlib.sha256(base_manifest.read_bytes()).hexdigest(),
            "base_payload_sha256": base_payload_record["sha256"],
            "base_payload_size": base_payload_record["size"],
            "build_identity": build_identity,
            "component": component,
            "component_version": component_version,
            "feature_id": feature_id,
            "files": files,
            "format": "squashfs-lz4",
            "kind": "runtime-capsule",
            "max_bytes": max_bytes,
            "payload": {"filename": output.name, "sha256": payload_hash, "size": payload_size},
            "product_release": product_release,
            "schema_version": 1,
            "service_dependencies": service_dependencies,
            "source_commit": source_commit,
            "compatibility": compatibility,
        }
        manifest_path.write_bytes((json.dumps(runtime_manifest, indent=2, sort_keys=True) + "\n").encode())
        manifest_written = True
        from verify_runtime import verify_capsule
        verify_capsule(
            feature_id,
            base_payload,
            base_manifest,
            product_release,
            source_commit,
            component,
            component_version,
            build_identity,
            service_dependencies,
            compatibility,
            output,
            manifest_path,
            max_bytes,
        )
        return runtime_manifest
    except Exception:
        if staged_output is not None:
            staged_output.unlink(missing_ok=True)
        if output_written or os.path.lexists(output):
            output.unlink(missing_ok=True)
        if manifest_written or os.path.lexists(manifest_path):
            manifest_path.unlink(missing_ok=True)
        raise


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
    parser.add_argument("--replacement", action="append", required=True)
    parser.add_argument("--max-bytes", type=int, required=True, help="trusted positive capsule size cap in bytes")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        compatibility = strict_json(args.compatibility.encode(), "compatibility")
        if not isinstance(compatibility, dict):
            raise error("compatibility constraints must be an object")
        build_capsule(
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
            args.replacement,
            args.output,
            args.manifest,
            args.max_bytes,
        )
    except (CapsuleError, OSError, ValueError) as exc:
        print(f"ERROR: {str(exc)[:240]}", file=sys.stderr)
        return 1
    print(f"capsule={args.output}")
    print(f"manifest={args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

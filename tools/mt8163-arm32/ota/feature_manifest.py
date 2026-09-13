#!/usr/bin/env python3
"""Canonical signed manifest and control-tar contract for reboot-bound OTA v2."""
from __future__ import annotations

import hashlib
import io
import re
import tarfile
from typing import Any

from nacl.signing import SigningKey, VerifyKey

BOOT_SIZE = 16 * 1024 * 1024
MAX_MANIFEST_SIZE = 64 * 1024
MAX_CONTROL_TAR_SIZE = BOOT_SIZE + MAX_MANIFEST_SIZE + 8 * 512
FEATURE_ORDER = ("airplay2", "tts", "wakeword", "stt", "assistant")
SERVICE_PROFILES = frozenset(("diagnostic", "production"))
FEATURE_POLICIES = frozenset(("preserve", "redistributable", "community-noncommercial", "exclude"))
DAEMON_PATHS = {
    "airplay2": "usr/local/sbin/libreecho-audio-engine",
    "tts": "usr/local/sbin/libreecho-ttsd",
    "wakeword": "usr/local/sbin/libreecho-waked",
    "stt": "usr/local/sbin/libreecho-sttd",
    "assistant": "usr/local/sbin/libreecho-agentd",
}
HASH = re.compile(r"[0-9a-f]{64}\Z")
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~:-]{0,95}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")
TOP = (
    "format", "manifest_version", "board", "soc", "architecture", "image_profile",
    "transaction_type", "transaction_id", "version", "update_channel", "service_profile",
    "feature_policy", "minimum_updater_schema", "feature_asset_base", "commit_policy", "feature_ids",
    "boot_filename", "boot_size", "boot_sha256",
)


class ContractError(ValueError):
    """Raised for a malformed or incompatible signed OTA contract."""


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _hash(value: Any, label: str) -> None:
    _need(isinstance(value, str) and HASH.fullmatch(value) is not None, f"invalid {label}")


def _size(value: Any, label: str) -> None:
    _need(isinstance(value, int) and not isinstance(value, bool) and 0 <= value < (1 << 63), f"invalid {label}")


def _token(value: Any, label: str) -> None:
    _need(isinstance(value, str) and TOKEN.fullmatch(value) is not None, f"invalid {label}")


def _asset(value: Any, label: str, suffix: str) -> None:
    _need(isinstance(value, str) and ASSET.fullmatch(value) is not None and not value.startswith("."), f"invalid {label}")
    _need("/" not in value and "\\" not in value and value.endswith(suffix), f"invalid {label}")


def _feature_prefix(record: dict[str, Any]) -> str:
    return f"libreecho-radar-puffin-{record['release']}-{record['feature_id']}"


def _validate_record(record: Any) -> None:
    _need(isinstance(record, dict), "feature record must be an object")
    required = {"feature_id", "action", "activation", "base_payload_sha256", "base_manifest_sha256", "daemon_path", "daemon_sha256", "release", "source_commit"}
    _need(set(record).issuperset(required), "feature fields missing")
    fid, action, activation = record["feature_id"], record["action"], record["activation"]
    _need(fid in FEATURE_ORDER, "invalid feature id")
    _need(action in {"preserve", "runtime", "replace"}, "invalid feature action")
    _need(activation == "reboot", "v2 requires reboot activation")
    _hash(record["base_payload_sha256"], "base payload hash")
    _hash(record["base_manifest_sha256"], "base manifest hash")
    _need(record["daemon_path"] == DAEMON_PATHS[fid], "invalid daemon path")
    _hash(record["daemon_sha256"], "daemon hash")
    _token(record["release"], "release")
    _need(isinstance(record["source_commit"], str) and COMMIT.fullmatch(record["source_commit"]) is not None, "invalid source commit")
    forbidden = {"previous_daemon_sha256", "feature_version", "compatible_os_version", "service_abi", "service_dependencies"}
    _need(not (set(record) & forbidden), "unsupported feature field")
    if action == "preserve":
        _need(set(record) == required, "preserve feature fields do not match action")
        return
    fields = required | {"asset", "size", "sha256", "manifest_asset", "manifest_size", "manifest_sha256"}
    _need(set(record) == fields, "feature fields do not match action")
    suffix = ".runtime.squashfs" if action == "runtime" else ".payload.squashfs"
    manifest_suffix = ".runtime-manifest.json" if action == "runtime" else ".manifest.json"
    _asset(record["asset"], "payload asset", suffix)
    _asset(record["manifest_asset"], "feature manifest asset", manifest_suffix)
    _need(record["asset"] == _feature_prefix(record) + suffix, "payload asset identity mismatch")
    _need(record["manifest_asset"] == _feature_prefix(record) + manifest_suffix, "manifest asset identity mismatch")
    _size(record["size"], "payload size")
    _size(record["manifest_size"], "manifest size")
    _hash(record["sha256"], "payload hash")
    _hash(record["manifest_sha256"], "manifest hash")


def validate_manifest(manifest: dict[str, Any]) -> None:
    _need(isinstance(manifest, dict), "manifest must be an object")
    _need(set(manifest).issuperset(set(TOP) | {"features"}), "manifest fields missing")
    _need(manifest["format"] == "libreecho-ota-v2", "invalid manifest format")
    _need(manifest["manifest_version"] == 1 and isinstance(manifest["manifest_version"], int), "unsupported manifest version")
    _need(manifest["board"] == "radar_puffin" and manifest["soc"] == "mt8163" and manifest["architecture"] == "armv7", "invalid target")
    _need(manifest["image_profile"] == "ota", "invalid image profile")
    _need(manifest["transaction_type"] == "system", "v2 only supports system transactions")
    for key in ("transaction_id", "version", "update_channel", "service_profile", "feature_policy", "feature_asset_base", "commit_policy"):
        _token(manifest[key], key)
    _need(manifest["update_channel"] in {"dev", "stable"}, "invalid update channel")
    _need(manifest["service_profile"] in SERVICE_PROFILES, "invalid service profile")
    _need(manifest["feature_policy"] in FEATURE_POLICIES, "invalid feature policy")
    if manifest["feature_policy"] == "exclude":
        _need(manifest["service_profile"] == "diagnostic", "feature exclusion requires diagnostic service profile")
    if manifest["feature_policy"] in {"redistributable", "community-noncommercial"}:
        _need(manifest["service_profile"] == "production", "redistributable feature policy requires production service profile")
    _need(manifest["feature_asset_base"] == "github-release-channel", "invalid feature asset base")
    _need(manifest["commit_policy"] == "after-slot-confirm", "invalid commit policy")
    _need(manifest["minimum_updater_schema"] == 2 and isinstance(manifest["minimum_updater_schema"], int), "unsupported updater schema")
    _need(manifest["boot_filename"] == "boot.img", "boot image is required")
    _size(manifest["boot_size"], "boot size")
    _need(manifest["boot_size"] == BOOT_SIZE, "boot size must be 16 MiB")
    _hash(manifest["boot_sha256"], "boot hash")
    records = manifest["features"]
    _need(isinstance(records, list), "features must be a list")
    ids = [r.get("feature_id") if isinstance(r, dict) else None for r in records]
    _need(all(i in FEATURE_ORDER for i in ids), "invalid feature id")
    _need(len(ids) == len(set(ids)) and ids == sorted(ids, key=FEATURE_ORDER.index), "feature records are not canonical")
    _need(ids == list(FEATURE_ORDER), "feature set is incomplete")
    for record in records:
        _validate_record(record)


def _pairs(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    top = [(key, str(manifest[key])) for key in TOP if key != "feature_ids"]
    top.append(("feature_ids", ",".join(r["feature_id"] for r in manifest["features"])))
    out = top
    for r in manifest["features"]:
        prefix = "feature_" + r["feature_id"] + "_"
        names = ("action", "activation", "base_payload_sha256", "base_manifest_sha256", "daemon_path", "daemon_sha256", "release", "source_commit")
        out.extend((prefix + name, str(r[name])) for name in names)
        if r["action"] != "preserve":
            out.extend((prefix + name, str(r[name])) for name in ("asset", "size", "sha256", "manifest_asset", "manifest_size", "manifest_sha256"))
    return out


def serialize_manifest(manifest: dict[str, Any]) -> bytes:
    validate_manifest(manifest)
    raw = "".join(f"{k}={v}\n" for k, v in _pairs(manifest)).encode("ascii")
    _need(len(raw) <= MAX_MANIFEST_SIZE, "manifest is too large")
    return raw


def _parse_lines(data: bytes) -> dict[str, str]:
    _need(isinstance(data, bytes) and len(data) <= MAX_MANIFEST_SIZE and data.endswith(b"\n"), "malformed manifest")
    _need(b"\r" not in data and b"\x00" not in data, "malformed manifest")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ContractError("manifest is not ASCII") from exc
    values: dict[str, str] = {}
    for line in text[:-1].split("\n"):
        _need(line and line.count("=") == 1, "malformed manifest line")
        key, value = line.split("=", 1)
        _need(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) is not None and value != "", "malformed manifest field")
        _need(key not in values, "duplicate manifest field")
        values[key] = value
    return values


def parse_manifest(data: bytes) -> dict[str, Any]:
    values = _parse_lines(data)
    _need(values.get("format") == "libreecho-ota-v2", "invalid manifest format")
    features: list[dict[str, Any]] = []
    ids = values.get("feature_ids", "").split(",") if values.get("feature_ids", "") else []
    _need(all(i in FEATURE_ORDER for i in ids) and ids == sorted(ids, key=FEATURE_ORDER.index), "invalid feature ids")
    _need(ids == list(FEATURE_ORDER), "feature set is incomplete")
    known = set(TOP)
    records: dict[str, dict[str, Any]] = {i: {"feature_id": i} for i in ids}
    for key, value in values.items():
        if key in known:
            continue
        match = re.fullmatch(r"feature_(airplay2|tts|wakeword|stt|assistant)_([a-z][a-z0-9_]*)", key)
        _need(match is not None and match.group(1) in records, "unknown manifest field")
        records[match.group(1)][match.group(2)] = value
    int_keys = {"manifest_version", "minimum_updater_schema", "boot_size", "size", "manifest_size"}
    for record in records.values():
        for key in set(record):
            if key in int_keys:
                try: record[key] = int(record[key])
                except (TypeError, ValueError) as exc: raise ContractError(f"invalid {key}") from exc
        features.append(record)
    result: dict[str, Any] = {k: values[k] for k in TOP}
    for key in ("manifest_version", "minimum_updater_schema", "boot_size"):
        result[key] = int(result[key])
    result["features"] = features
    validate_manifest(result)
    _need(serialize_manifest(result) == data, "manifest is not canonical")
    return result


def _key(value: SigningKey | VerifyKey | bytes | str) -> SigningKey | VerifyKey:
    if isinstance(value, (SigningKey, VerifyKey)): return value
    if isinstance(value, str): value = bytes.fromhex(value)
    _need(isinstance(value, bytes) and len(value) == 32, "invalid key")
    try: return SigningKey(value)
    except (TypeError, ValueError): return VerifyKey(value)


def sign_manifest(manifest: dict[str, Any], signing_key: SigningKey | bytes | str) -> bytes:
    return _key(signing_key).sign(serialize_manifest(manifest)).signature.hex().encode("ascii") + b"\n"  # type: ignore[union-attr]


def build_control_tar(manifest: dict[str, Any], boot_image: bytes, signing_key: SigningKey | bytes | str) -> bytes:
    raw = serialize_manifest(manifest)
    _need(isinstance(boot_image, bytes) and len(boot_image) == BOOT_SIZE and boot_image.startswith(b"ANDROID!"), "invalid boot image")
    _need(hashlib.sha256(boot_image).hexdigest() == manifest["boot_sha256"], "boot image hash mismatch")
    sig = sign_manifest(manifest, signing_key)
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in (("manifest", raw), ("manifest.sig", sig), ("boot.img", boot_image)):
            info = tarfile.TarInfo(name); info.size = len(data); info.mode = 0o644; info.uid = info.gid = 0; info.uname = info.gname = "root"; info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    result = out.getvalue()
    _need(len(result) <= MAX_CONTROL_TAR_SIZE, "control tar is too large")
    return result

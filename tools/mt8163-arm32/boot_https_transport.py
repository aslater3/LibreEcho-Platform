"""Boot-local HTTPS repair closure, copied from the reviewed static assistant client.

Extraction happens only on the build host. No feature mount, loader, shared
library or userdata path is required to run the shipped /usr/bin/curl.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

SCHEMA = "libreecho-boot-https-transport/v1"
CA_SHA256 = "c0c940a0e30d859783f7f130868d8082e79936ff0b41a0b1098ac7f98909263b"
CURL_SOURCE_SHA256 = "aa1b66a70eace83dc624508745646c08ae561de512ab403adffb93ac87fc72e6"
MEMBERS = {
    "usr/bin/curl": "usr/local/libexec/libreecho-curl",
    "etc/ssl/certs/ca-certificates.crt": "usr/local/share/libreecho/cacert.pem",
    "usr/local/share/licenses/curl/COPYING": "usr/local/share/licenses/curl/COPYING",
    "usr/local/share/licenses/ca-certificates/copyright": "usr/local/share/licenses/ca-certificates/copyright",
}
for _name in ("THIRD_PARTY_NOTICES.txt", "OpenSSL-copyright", "glibc-copyright",
              "gcc-runtime-copyright", "LGPL-2.1.txt", "GPL-3.0.txt"):
    _path = "usr/local/share/licenses/libreecho-assistant/" + _name
    MEMBERS[_path] = _path


def stage_transport(stage: Path, payload: Path, payload_hash: str,
                    payload_manifest_hash: str, source_files: dict,
                    qemu_arm: str) -> dict:
    # Check the byte-level ABI before the bounded QEMU capability probe.
    from verify_recovery_image import elf_info

    staged = {}
    contents = {}
    for target, source in MEMBERS.items():
        mode = "0755" if target == "usr/bin/curl" else "0644"
        record = source_files.get(source)
        if (not isinstance(record, dict) or record.get("mode") != mode or
                type(record.get("size")) is not int or record["size"] <= 0):
            raise SystemExit(f"ERROR: boot HTTPS source member record is invalid: {source}")
        try:
            # -cat does not materialize untrusted paths or symlinks in the host tree.
            result = subprocess.run(["unsquashfs", "-cat", str(payload), source],
                                    capture_output=True, check=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SystemExit(f"ERROR: cannot extract boot HTTPS source member: {source}") from exc
        data = result.stdout
        digest = hashlib.sha256(data).hexdigest()
        if digest != record.get("sha256") or len(data) != record["size"]:
            raise SystemExit(f"ERROR: boot HTTPS source member identity mismatch: {source}")
        if target == "usr/bin/curl" and elf_info(data) != (1, 40, 0x05000400, None, (), False):
            raise SystemExit("ERROR: boot HTTPS curl must be static ARM32 hard-float")
        if target.startswith("etc/ssl/") and digest != CA_SHA256:
            raise SystemExit("ERROR: boot HTTPS CA bundle is not the reviewed trust store")
        destination = stage / target
        if destination.exists() or destination.is_symlink():
            raise SystemExit(f"ERROR: boot HTTPS member collides with existing image: {target}")
        # Reject ancestors supplied as symlinks by another bundle.
        for parent in destination.parents:
            if parent == stage:
                break
            if parent.is_symlink():
                raise SystemExit(f"ERROR: boot HTTPS member has a symlink parent: {target}")
        contents[target] = data
        staged[target] = dict(source_member=source, sha256=digest, size=len(data), mode=mode)
    # Validate the entire closure before writing any of it.
    for target, data in contents.items():
        destination = stage / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        destination.chmod(int(staged[target]["mode"], 8))
    try:
        probe = subprocess.run([qemu_arm, str(stage / "usr/bin/curl"), "--version"],
                               capture_output=True, text=True, check=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit("ERROR: boot HTTPS client capability probe failed") from exc
    lines = probe.stdout.splitlines()
    if (not lines or not lines[0].startswith("curl 8.21.0 ") or
            "libcurl/8.21.0" not in lines[0] or "OpenSSL/3.0.13" not in lines[0] or
            "Protocols: http https" not in lines or
            not any(line.startswith("Features:") and "SSL" in line.split()[1:] for line in lines)):
        raise SystemExit("ERROR: boot HTTPS client lacks reviewed HTTPS/TLS capabilities")
    return dict(schema=SCHEMA, payload_dependency=False, userdata_dependency=False,
                capabilities=dict(curl="8.21.0", tls="OpenSSL/3.0.13", protocols=["http", "https"]),
                client="/usr/bin/curl", ca="/etc/ssl/certs/ca-certificates.crt",
                curl_source_sha256=CURL_SOURCE_SHA256, source_payload_sha256=payload_hash,
                source_manifest_sha256=payload_manifest_hash, files=staged)

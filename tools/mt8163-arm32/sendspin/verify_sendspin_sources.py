#!/usr/bin/env python3
"""LibreEcho Sendspin — byte-level pinned source identity verifier.

The Sendspin build must prove that the *actual bytes* it compiles are the ones
SOURCE.lock pins. Directory names, user-supplied revision strings, and
declared-but-unchecked hashes are not identity: a staged tree named after the
right commit can contain anything.

This module therefore roots identity in the SHA-256 of the immutable upstream
archive bytes. Two modes:

  * regenerate — ``--archive-dir DIR --stage-out OUT --receipt FILE``: hash every
    pinned archive, verify it against SOURCE.lock, extract the verified archive
    into an isolated scratch tree, and emit a stage receipt that binds each
    archive digest to the tree it produced.

  * verify — ``--archive-dir DIR --staged-dir DIR --receipt FILE``: re-derive the
    pinned tree from every hash-verified archive and require the staged tree to be
    byte-identical to it. ``--archive-dir`` is mandatory here: a receipt that merely
    *declares* a digest (even one consistent with a tampered tree) cannot prove
    archive identity, so it is only ever accepted when it agrees with the bytes
    re-derived from the pinned archives.

A "complete tree receipt" is required: every SOURCE.lock dependency and every
declared submodule must be covered, or verification fails closed.

Every digest-pinned input (archive, patch) is opened once with no symlink
following and only its verified bytes are ever parsed, applied or extracted;
the digest always covers the exact bytes that are consumed.  A path swap behind
the verifier can therefore only make verification fail, never substitute
unverified content.

No network access, no privileged operations, no changes outside the paths given.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import NamedTuple

SCHEMA = "libreecho-sendspin-stage-receipt/1"
PATCH_SCHEMA = "libreecho-sendspin-patch-inventory/1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Input handling bounds: pinned archives are consumed through a single
# descriptor.  Small archives become an immutable in-memory verified buffer;
# larger archives are stream-copied into an anonymous private snapshot, so peak
# memory stays bounded regardless of archive size.  Patch bytes are small and
# always frozen in memory.
READ_CHUNK = 1 << 20
ARCHIVE_BUFFER_LIMIT = 64 << 20
PATCH_BYTE_LIMIT = 8 << 20


class SourceVerificationError(RuntimeError):
    """Raised when staged source identity does not match SOURCE.lock."""


def _open_regular_nofollow(path: Path, what: str) -> int:
    """Open ``path`` once, refusing symlinks and non-regular files.

    The returned descriptor is the only handle used for the bytes; callers must
    close it.  ``O_NOFOLLOW`` makes a swapped symlink fail closed instead of
    redirecting the read, and ``O_NONBLOCK`` keeps a hostile FIFO from blocking
    the open (a FIFO/device/directory is then refused by ``fstat``).
    """
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SourceVerificationError(
            f"{what} {path}: cannot open as a regular file ({exc.strerror})") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SourceVerificationError(f"{what} {path}: not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, READ_CHUNK)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    """SHA-256 of a regular file, read once through a no-follow descriptor."""
    fd = _open_regular_nofollow(Path(path), "file")
    try:
        return _hash_fd(fd)
    finally:
        os.close(fd)


class VerifiedArchive:
    """An archive reduced to bytes whose SHA-256 was checked before use."""

    __slots__ = ("handle", "name", "sha256")

    def __init__(self, handle, name: str, sha256: str) -> None:
        self.handle = handle  # binary file object positioned at byte 0
        self.name = name
        self.sha256 = sha256

    def close(self) -> None:
        self.handle.close()


def open_verified_archive(archive: Path, expected_sha256: str) -> VerifiedArchive:
    """Read ``archive`` exactly once and return only digest-verified bytes.

    The digest covers precisely the bytes that will be extracted: a small
    archive lives in an immutable in-memory buffer; a larger one is stream-copied
    into an anonymous private snapshot (never a predictable shared tmp path) and
    the digest is taken over the copied bytes.  A writer that mutates the file in
    place or repoints a symlink between the digest check and the extraction can
    therefore only make verification fail, never change what is consumed.
    """
    archive = Path(archive)
    fd = _open_regular_nofollow(archive, "archive")
    try:
        digest = hashlib.sha256()
        buffered = bytearray()
        while len(buffered) <= ARCHIVE_BUFFER_LIMIT:
            chunk = os.read(fd, READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            buffered += chunk
        if len(buffered) <= ARCHIVE_BUFFER_LIMIT:
            actual = digest.hexdigest()
            if actual != expected_sha256:
                raise SourceVerificationError(
                    f"archive {archive.name}: sha256 {actual} != lock {expected_sha256}")
            return VerifiedArchive(io.BytesIO(bytes(buffered)), archive.name, actual)
        snapshot = tempfile.TemporaryFile(prefix="sendspin-archive-")
        try:
            snapshot.write(buffered)
            del buffered
            while True:
                chunk = os.read(fd, READ_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                snapshot.write(chunk)
            actual = digest.hexdigest()
            if actual != expected_sha256:
                raise SourceVerificationError(
                    f"archive {archive.name}: sha256 {actual} != lock {expected_sha256}")
            snapshot.seek(0)
            return VerifiedArchive(snapshot, archive.name, actual)
        except BaseException:
            snapshot.close()
            raise
    finally:
        os.close(fd)


def load_lock(path: Path) -> dict:
    """Return {name: entry} for every pinned dependency + submodule in the lock.

    The lock's ``identity`` block (spec/SDK/oracle) and its ``dependencies`` list
    are flattened; each dependency's submodules become ``name::subpath`` entries.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    entries: dict[str, dict] = {}

    def add(name: str, record: dict, submodule_paths: list[str] | None = None,
            staged_path: str = "") -> None:
        commit = record.get("commit", "")
        digest = record.get("archive_sha256", "")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise SourceVerificationError(f"{name}: lock entry has no 40-hex commit")
        if not HEX64.fullmatch(digest):
            raise SourceVerificationError(f"{name}: lock entry has no 64-hex archive digest")
        entries[name] = {
            "name": name,
            "path": staged_path,
            "commit": commit,
            "archive_sha256": digest,
            "archive_url": record.get("archive_url", ""),
            "submodule_paths": list(submodule_paths or []),
        }

    for key, record in sorted(data.get("identity", {}).items()):
        if isinstance(record, dict):
            add(f"identity::{key}", record, staged_path=f"identity/{key}")
    for record in data.get("dependencies", []):
        name = record["name"]
        submodule_paths = [sub["path"] for sub in record.get("submodules", [])]
        add(name, record, submodule_paths, staged_path=name)
        for sub in record.get("submodules", []):
            add(f"{name}::{sub['path']}", sub, staged_path=f"{name}/{sub['path']}")
    if not entries:
        raise SourceVerificationError(f"{path}: lock declares no pinned sources")
    return entries


def find_archive(archive_dir: Path, commit: str) -> Path:
    """Locate the single regular-file archive whose name embeds ``commit``."""
    matches = sorted(p for p in Path(archive_dir).rglob("*.tar.gz") if commit in p.name)
    if not matches:
        raise SourceVerificationError(f"no staged archive embeds commit {commit} under {archive_dir}")
    for match in matches:
        try:
            st = os.lstat(match)
        except OSError as exc:
            raise SourceVerificationError(
                f"staged archive {match}: cannot stat ({exc.strerror})") from exc
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise SourceVerificationError(f"staged archive {match.name}: not a regular file")
    if len(matches) > 1:
        raise SourceVerificationError(
            f"ambiguous staged archives for commit {commit}: {[p.name for p in matches]}"
        )
    return matches[0]


def verify_archive_bytes(archive: Path, expected_sha256: str) -> str:
    """Hash-verify an archive once and return the verified digest."""
    verified = open_verified_archive(archive, expected_sha256)
    try:
        return verified.sha256
    finally:
        verified.close()


def tree_digest(root: Path, exclude: tuple[str, ...] = ()) -> tuple[str, int]:
    """Canonical digest over a staged tree: path + mode + content hash.

    Symlinks are recorded by target, not followed, so a tree cannot smuggle
    identity by pointing elsewhere.  Regular files are read once through a
    no-follow descriptor and the hash covers exactly the bytes read. ``exclude``
    skips declared submodule subtrees so a parent digest does not depend on when
    its children landed.
    """
    root = Path(root)
    entries: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        for name in dirnames + filenames:
            entries.append(base / name)
    digest = hashlib.sha256()
    count = 0
    for path in sorted(entries):
        rel = path.relative_to(root).as_posix()
        if any(rel == sub or rel.startswith(sub + "/") for sub in exclude):
            continue
        try:
            st = os.lstat(path)
        except OSError as exc:
            raise SourceVerificationError(
                f"staged tree entry {rel}: cannot stat ({exc.strerror})") from exc
        if stat.S_ISLNK(st.st_mode):
            digest.update(f"L\0{rel}\0{os.readlink(path)}\n".encode())
            count += 1
        elif stat.S_ISREG(st.st_mode):
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
            except OSError as exc:
                raise SourceVerificationError(
                    f"staged tree entry {rel}: cannot open ({exc.strerror})") from exc
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise SourceVerificationError(
                        f"staged tree entry {rel}: changed while hashing")
                digest.update(f"F\0{rel}\0{oct(st.st_mode & 0o777)}\0".encode())
                digest.update(bytes.fromhex(_hash_fd(fd)) + b"\n")
            finally:
                os.close(fd)
            count += 1
        elif stat.S_ISDIR(st.st_mode):
            continue
        else:
            raise SourceVerificationError(
                f"staged tree entry {rel}: not a regular file, directory or symlink")
    if count == 0:
        raise SourceVerificationError(f"staged tree is empty: {root}")
    return digest.hexdigest(), count


def _assert_tree_confined(root: Path) -> None:
    """Refuse an unsafe transformed tree before it is published or accepted.

    No entry may be a symlinked directory, carry an absolute or tree-escaping
    symlink body, hold setuid/setgid/sticky mode bits, or be a non-regular
    non-directory file.  The walk uses ``lstat`` and never follows a link, so the
    verdict describes the tree itself, not whatever a link points at.
    """
    root = Path(root)
    try:
        st = os.lstat(root)
    except OSError as exc:
        raise SourceVerificationError(f"staged tree {root}: cannot stat ({exc.strerror})") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise SourceVerificationError(f"staged tree root is not a real directory: {root}")
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack:
        current, rel_dir = stack.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                    st = entry.stat(follow_symlinks=False)
                    mode = st.st_mode
                    if stat.S_ISLNK(mode):
                        body = os.readlink(entry.path)
                        if os.path.isabs(body):
                            raise SourceVerificationError(
                                f"staged tree {rel}: absolute symlink target {body!r}")
                        target = os.path.normpath(os.path.join(rel_dir, body))
                        if target == ".." or target.startswith("../") or os.path.isabs(target):
                            raise SourceVerificationError(
                                f"staged tree {rel}: symlink body {body!r} escapes the tree")
                        if entry.is_dir(follow_symlinks=True):
                            raise SourceVerificationError(
                                f"staged tree {rel}: symlinked directory is refused")
                        continue
                    if mode & 0o7000:
                        raise SourceVerificationError(
                            f"staged tree {rel}: unsafe mode {oct(mode & 0o7777)}")
                    if stat.S_ISDIR(mode):
                        stack.append((Path(entry.path), rel))
                        continue
                    if stat.S_ISREG(mode):
                        continue
                    raise SourceVerificationError(
                        f"staged tree {rel}: not a regular file, directory or symlink")
        except OSError as exc:
            raise SourceVerificationError(
                f"staged tree {rel_dir or '.'}: cannot scan ({exc.strerror})") from exc


def _require_real_directory_chain(staged_root: Path, rel: str) -> None:
    """Require every component of a declared staged path to be a real directory."""
    current = Path(staged_root)
    for part in PurePosixPath(rel).parts:
        current = current / part
        try:
            st = os.lstat(current)
        except OSError as exc:
            raise SourceVerificationError(
                f"staged path {rel!r}: cannot stat {current} ({exc.strerror})") from exc
        if stat.S_ISLNK(st.st_mode):
            raise SourceVerificationError(
                f"staged path {rel!r}: component {current} is a symlink")
        if not stat.S_ISDIR(st.st_mode):
            raise SourceVerificationError(
                f"staged path {rel!r}: component {current} is not a directory")


def _resolve_within(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root`` and refuse any path that escapes it.

    The staged sub-path comes from the lock/receipt; a hostile entry must never be
    able to redirect verification at an arbitrary filesystem location.
    """
    root_resolved = Path(root).resolve()
    candidate = (root_resolved / rel).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise SourceVerificationError(
            f"staged path {rel!r} escapes staged root {root_resolved}")
    return candidate


def _extract(source: VerifiedArchive, destination: Path) -> None:
    """Extract a verified archive through its frozen byte source.

    ``source`` is the digest-verified ``VerifiedArchive``; extraction never
    reopens the archive path, so a swapped path cannot change the bytes.
    """
    if destination.exists():
        # A submodule path can arrive as an empty placeholder inside its parent
        # archive; overwriting real content is still refused.
        if any(destination.iterdir()):
            raise SourceVerificationError(f"refusing to overwrite staged tree: {destination}")
    else:
        destination.mkdir(parents=True)
    with tarfile.open(fileobj=source.handle, mode="r:gz") as handle:
        handle.extractall(destination, filter="data")
    # GitHub archives nest everything under a single <repo>-<commit>/ directory.
    children = list(destination.iterdir())
    if len(children) == 1 and children[0].is_dir() and not children[0].is_symlink():
        nested = children[0]
        for child in nested.iterdir():
            child.rename(destination / child.name)
        nested.rmdir()


class VerifiedPatch(NamedTuple):
    """A declared patch reduced to immutable bytes whose SHA-256 was checked."""

    name: str
    path: Path
    sha256: str
    data: bytes


def _read_verified_patch(path: Path, expected_sha256: str) -> bytes:
    """Read a patch once (bounded) and return only its digest-verified bytes."""
    path = Path(path)
    fd = _open_regular_nofollow(path, "patch")
    try:
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > PATCH_BYTE_LIMIT:
                raise SourceVerificationError(
                    f"patch {path.name}: larger than {PATCH_BYTE_LIMIT} bytes; refused")
            digest.update(chunk)
            chunks.append(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    actual = digest.hexdigest()
    if actual != expected_sha256:
        raise SourceVerificationError(
            f"patch {path.name}: sha256 {actual} != inventory {expected_sha256}")
    return data


def _scan_patch_dir(patch_dir: Path) -> dict[str, tuple[Path, os.stat_result]] | None:
    """Return every directory entry as name -> (path, lstat), or None if absent."""
    try:
        entries: dict[str, tuple[Path, os.stat_result]] = {}
        with os.scandir(patch_dir) as iterator:
            for entry in iterator:
                entries[entry.name] = (Path(entry.path), entry.stat(follow_symlinks=False))
        return entries
    except FileNotFoundError:
        return None
    except NotADirectoryError as exc:
        raise SourceVerificationError(
            f"patch directory {patch_dir} is not a directory") from exc


def load_patch_inventory(lock: dict, lock_path: Path, patch_dir: Path) -> dict[str, list[VerifiedPatch]]:
    """Validate SOURCE.lock's ``patch_inventory`` and freeze each patch's bytes.

    The inventory is closed and single-apply: the patch directory is enumerated
    closed (a declared-but-missing, undeclared, dotfile, subdirectory, symlink or
    otherwise non-regular entry is refused), and every declared patch is opened
    once with no symlink following.  The digest covers exactly the bytes that are
    returned, and those frozen bytes are the only bytes ever parsed or applied.
    """
    data = json.loads(Path(lock_path).read_text(encoding="utf-8"))
    inventory = data.get("patch_inventory", {})
    applied = inventory.get("applied", [])
    if not isinstance(applied, list):
        raise SourceVerificationError("patch_inventory.applied must be a list")
    if applied and inventory.get("schema") != PATCH_SCHEMA:
        raise SourceVerificationError(
            f"patch_inventory.schema must be {PATCH_SCHEMA!r} when patches are applied")

    records: list[tuple[str, str, str]] = []
    for index, record in enumerate(applied):
        if not isinstance(record, dict):
            raise SourceVerificationError(f"patch_inventory.applied[{index}] is not an object")
        name = record.get("file", "")
        digest = record.get("sha256", "")
        target = record.get("target", "")
        # Bare filename only: no directory components, no traversal, no dotfile.
        if (not isinstance(name, str) or not name.endswith(".patch") or "/" in name
                or "\\" in name or name in (".", "..") or name.startswith(".")):
            raise SourceVerificationError(
                f"patch_inventory.applied[{index}].file is not a bare *.patch name: {name!r}")
        if not isinstance(digest, str) or not HEX64.fullmatch(digest):
            raise SourceVerificationError(f"patch_inventory.applied[{index}].sha256 is not 64-hex")
        if target not in lock:
            raise SourceVerificationError(
                f"patch_inventory.applied[{index}].target {target!r} is not a pinned lock source")
        # If the entry names the pristine archive it applies to, it must be the target's
        # locked archive: a patch cannot silently re-anchor onto a different source.
        pristine = record.get("pristine_archive_sha256")
        if pristine is not None and pristine != lock[target]["archive_sha256"]:
            raise SourceVerificationError(
                f"patch_inventory.applied[{index}].pristine_archive_sha256 {pristine} != "
                f"{target}'s locked archive {lock[target]['archive_sha256']}")
        records.append((name, digest, target))

    declared = [name for name, _, _ in records]
    if len(set(declared)) != len(declared):
        raise SourceVerificationError("patch_inventory declares the same patch file more than once")

    patch_dir = Path(patch_dir)
    entries = _scan_patch_dir(patch_dir)
    on_disk = set(entries) if entries is not None else set()
    missing = sorted(set(declared) - on_disk)
    extra = sorted(on_disk - set(declared))
    if missing or extra:
        raise SourceVerificationError(f"patch inventory mismatch: missing={missing} extra={extra}")
    if not on_disk:
        return {}

    by_target: dict[str, list[VerifiedPatch]] = {}
    for name, digest, target in records:
        path, st = entries[name]  # type: ignore[index]
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise SourceVerificationError(
                f"patch {name}: not a regular file (symlinks and special files are refused)")
        frozen = _read_verified_patch(path, digest)
        by_target.setdefault(target, []).append(VerifiedPatch(name, path, digest, frozen))
    return by_target


def _patch_targets(patch: VerifiedPatch) -> list[str]:
    """Return the file paths a unified diff addresses (its ---/+++ header lines)."""
    targets: list[str] = []
    for line in patch.data.decode("utf-8", errors="replace").splitlines():
        if line.startswith("--- ") or line.startswith("+++ "):
            targets.append(line[4:].split("\t", 1)[0].strip())
    return targets


def _run_patch(tree: Path, patch: VerifiedPatch, dry_run: bool) -> tuple[int, str, str]:
    """Apply the frozen verified patch bytes on stdin; the path is never reopened.

    ``-f`` keeps the application fully non-interactive and assumes a patch is
    *not* reversed, so an already-applied diff fails instead of being silently
    reversed into a second application.  The patch never travels through a
    filesystem path here, so a relative ``--patch-dir`` (or a path swapped after
    verification) cannot change which bytes are applied.
    """
    cmd = ["patch", "-p1", "-f", "--no-backup-if-mismatch"]
    if dry_run:
        cmd.append("--dry-run")
    cmd += ["-d", str(tree)]
    proc = subprocess.run(cmd, input=patch.data, capture_output=True)
    return (proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"))


def apply_patches(tree: Path, patches: list[VerifiedPatch]) -> None:
    """Apply each frozen patch to ``tree`` in order, refusing unsafe or non-single-apply diffs.

    ``-p1`` strips the ``a/``/``b/`` prefix the patch was generated with. Every target
    path is checked to stay inside the tree (no absolute path, no ``..``) both in the
    header and by application; each patch must apply cleanly exactly once, and a
    re-apply must fail (a diff that applies twice is not a deterministic transform).
    """
    tree = Path(tree)
    for patch in patches:
        for target in _patch_targets(patch):
            if target == "/dev/null":
                continue
            if target.startswith("/") or ".." in PurePosixPath(target).parts:
                raise SourceVerificationError(
                    f"patch {patch.name} targets unsafe path {target!r}")
        status, out, err = _run_patch(tree, patch, dry_run=True)
        if status != 0:
            raise SourceVerificationError(
                f"patch {patch.name} does not apply to the pinned tree: {out}{err}")
        status, out, err = _run_patch(tree, patch, dry_run=False)
        if status != 0:
            raise SourceVerificationError(
                f"patch {patch.name} failed to apply: {out}{err}")
        status, _, _ = _run_patch(tree, patch, dry_run=True)
        if status == 0:
            raise SourceVerificationError(
                f"patch {patch.name} applies a second time; the inventory is not a "
                f"single-apply transform")


def stage(lock: dict, archive_dir: Path, out_root: Path, receipt_path: Path,
          patch_map: dict[str, list[VerifiedPatch]] | None = None) -> dict:
    """Regenerate verified sources in an isolated scratch tree + write a receipt."""
    if out_root.exists():
        raise SourceVerificationError(f"stage-out already exists: {out_root}")
    out_root.mkdir(parents=True)
    receipt = {"schema": SCHEMA, "complete": False, "entries": {}}
    for name, entry in lock.items():
        archive = find_archive(archive_dir, entry["commit"])
        verified = open_verified_archive(archive, entry["archive_sha256"])
        destination = out_root / entry["path"]
        try:
            _extract(verified, destination)
        finally:
            verified.close()
        if patch_map and name in patch_map:
            apply_patches(destination, patch_map[name])
        # The transformed tree is inspected before the receipt publishes it.
        _assert_tree_confined(destination)
        digest, count = tree_digest(destination, tuple(entry.get("submodule_paths", ())))
        receipt["entries"][name] = {
            "commit": entry["commit"],
            "archive_file": archive.name,
            "archive_sha256": entry["archive_sha256"],
            "tree_digest": digest,
            "file_count": count,
        }
    receipt["complete"] = set(receipt["entries"]) == set(lock)
    Path(receipt_path).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def verify_receipt(lock: dict, staged_root: Path, receipt_path: Path,
                   archive_dir: Path | None = None,
                   workdir: Path | None = None,
                   patch_map: dict[str, list[VerifiedPatch]] | None = None) -> dict:
    """Prove staged identity against the pinned archives, not the receipt.

    The receipt is untrusted input: it may declare any digest it likes. Identity
    is therefore re-derived from the hash-verified SOURCE.lock archives (reusing
    the extraction + tree-digest helpers), and the staged tree must be
    byte-identical to that pinned tree. The receipt's declared values must agree
    with the *pinned* derivation; they are never used as the source of truth.

    ``archive_dir`` is required: without the archives there is no way to prove
    archive identity, so a receipt alone is refused.
    """
    if archive_dir is None:
        raise SourceVerificationError(
            "--archive-dir is required to verify a receipt: a receipt cannot prove "
            "archive identity from its own declared digests")
    receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    if receipt.get("schema") != SCHEMA:
        raise SourceVerificationError(f"unexpected receipt schema: {receipt.get('schema')!r}")
    entries = receipt.get("entries", {})
    if set(entries) != set(lock):
        missing = sorted(set(lock) - set(entries))
        extra = sorted(set(entries) - set(lock))
        raise SourceVerificationError(f"receipt closure mismatch: missing={missing} extra={extra}")
    if not receipt.get("complete"):
        raise SourceVerificationError("receipt does not claim a complete closure")

    staged_root = Path(staged_root)
    scratch = None
    if workdir is None:
        scratch = tempfile.TemporaryDirectory(prefix="sendspin-verify-")
        workdir = Path(scratch.name)
    else:
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)

    try:
        report: dict[str, str] = {}
        for name, entry in lock.items():
            recorded = entries[name]
            # The receipt's own declared identity must agree with the lock.
            if recorded.get("archive_sha256") != entry["archive_sha256"]:
                raise SourceVerificationError(f"{name}: receipt archive digest != lock")
            if recorded.get("commit") != entry["commit"]:
                raise SourceVerificationError(f"{name}: receipt commit != lock")
            # Locate and byte-verify the archive from the *lock* commit, never
            # from a receipt-supplied name (which could remap or traverse).  The
            # verified bytes are then the only bytes the derivation consumes.
            archive = find_archive(archive_dir, entry["commit"])
            verified = open_verified_archive(archive, entry["archive_sha256"])
            if recorded.get("archive_file") != archive.name:
                verified.close()
                raise SourceVerificationError(
                    f"{name}: receipt archive_file {recorded.get('archive_file')!r} "
                    f"!= pinned archive {archive.name!r}")
            # Re-derive the expected tree from the verified archive bytes.
            expected_root = workdir / ("exp_" + name.replace("::", "_").replace("/", "_"))
            if expected_root.exists():
                verified.close()
                raise SourceVerificationError(f"verify workdir collision: {expected_root}")
            try:
                _extract(verified, expected_root)
            finally:
                verified.close()
            if patch_map and name in patch_map:
                apply_patches(expected_root, patch_map[name])
            _assert_tree_confined(expected_root)
            exclude = tuple(entry.get("submodule_paths", ()))
            want_digest, want_count = tree_digest(expected_root, exclude)
            # The staged tree must be the pinned tree, and cannot escape the root.
            tree = _resolve_within(staged_root, entry["path"])
            _require_real_directory_chain(staged_root, entry["path"])
            digest, count = tree_digest(tree, exclude)
            if digest != want_digest:
                raise SourceVerificationError(
                    f"{name}: staged tree digest {digest} != pinned archive tree {want_digest}")
            _assert_tree_confined(tree)
            # The receipt must not merely declare a different value.
            if recorded.get("tree_digest") != want_digest:
                raise SourceVerificationError(
                    f"{name}: receipt tree_digest {recorded.get('tree_digest')} "
                    f"!= pinned archive tree {want_digest}")
            if count != want_count or recorded.get("file_count") != want_count:
                raise SourceVerificationError(
                    f"{name}: file count staged/receipt {count}/{recorded.get('file_count')} "
                    f"!= pinned {want_count}")
            report[name] = digest
        return report
    finally:
        if scratch is not None:
            scratch.cleanup()


def compare_staged_tree(lock: dict, archive_dir: Path, staged_root: Path, workdir: Path,
                        patch_map: dict[str, list[VerifiedPatch]] | None = None) -> dict:
    """Prove the staged tree's bytes equal the pinned archives, without trusting a receipt.

    Each pinned archive is hash-verified and re-extracted; the resulting tree digest
    is then matched against a subtree of ``staged_root``. Identity is byte-level and
    name-agnostic, so a differently-named Product stage is still accepted only when
    it carries the exact pinned bytes.  Candidate subtrees are never followed
    through symlinks, and a matched candidate must itself be a confined tree.
    """
    candidates: list[Path] = []
    staged_root = Path(staged_root)
    for top in sorted(staged_root.iterdir()):
        if not top.is_dir() or top.is_symlink():
            continue
        candidates.append(top)
        for child in sorted(top.iterdir()):
            if not child.is_dir() or child.is_symlink():
                continue
            candidates.append(child)
            for depth3 in sorted(child.iterdir()):
                if depth3.is_dir() and not depth3.is_symlink():
                    candidates.append(depth3)

    report: dict[str, str] = {}
    for name, entry in lock.items():
        archive = find_archive(archive_dir, entry["commit"])
        verified = open_verified_archive(archive, entry["archive_sha256"])
        extracted = workdir / ("cmp_" + name.replace("::", "_"))
        if extracted.exists():
            verified.close()
            raise SourceVerificationError(f"compare workdir collision: {extracted}")
        try:
            _extract(verified, extracted)
        finally:
            verified.close()
        if patch_map and name in patch_map:
            apply_patches(extracted, patch_map[name])
        _assert_tree_confined(extracted)
        exclude = tuple(entry.get("submodule_paths", ()))
        want, _ = tree_digest(extracted, exclude)
        match = None
        for candidate in candidates:
            try:
                have, _ = tree_digest(candidate, exclude)
            except SourceVerificationError:
                continue
            if have == want:
                match = candidate
                break
        if match is None:
            raise SourceVerificationError(
                f"{name}: no subtree of {staged_root} is byte-identical to pinned archive "
                f"{archive.name}")
        _assert_tree_confined(match)
        report[name] = str(match)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--archive-dir", type=Path)
    parser.add_argument("--stage-out", type=Path)
    parser.add_argument("--staged-dir", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--verify-only", action="store_true",
                        help="require and verify an existing receipt")
    parser.add_argument("--compare-tree", type=Path,
                        help="a staged source tree to validate byte-for-byte against the pinned archives")
    parser.add_argument("--compare-work", type=Path,
                        help="isolated scratch dir for --compare-tree re-extraction")
    parser.add_argument("--map-json", type=Path,
                        help="with --compare-tree, write {lock_name: staged_path} JSON")
    parser.add_argument("--patch-dir", type=Path,
                        help="directory of declared patch_inventory.applied patches "
                             "(default: <lock dir>/patches)")
    args = parser.parse_args(argv)

    lock = load_lock(args.lock)
    patch_dir = args.patch_dir if args.patch_dir is not None else args.lock.parent / "patches"
    try:
        patch_map = load_patch_inventory(lock, args.lock, patch_dir)
        if args.compare_tree is not None:
            if args.archive_dir is None or args.compare_work is None:
                parser.error("--compare-tree requires --archive-dir and --compare-work")
            report = compare_staged_tree(lock, args.archive_dir, args.compare_tree,
                                         args.compare_work, patch_map)
            if args.map_json is not None:
                Path(args.map_json).write_text(
                    json.dumps({"entries": report}, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
            print(f"staged tree {args.compare_tree} is byte-identical to all "
                  f"{len(report)} pinned archives")
            for name in sorted(report):
                print(f"  {name}: {report[name]}")
            return 0
        if args.stage_out is not None:
            if args.archive_dir is None or args.receipt is None:
                parser.error("--stage-out requires --archive-dir and --receipt")
            receipt = stage(lock, args.archive_dir, args.stage_out, args.receipt, patch_map)
            print(f"staged {len(receipt['entries'])} verified sources -> {args.stage_out}")
            for name in sorted(receipt["entries"]):
                print(f"  {name}: {receipt['entries'][name]['archive_sha256']}")
            return 0
        if args.verify_only:
            if args.staged_dir is None or args.receipt is None:
                parser.error("--verify-only requires --staged-dir and --receipt")
            if args.archive_dir is None:
                parser.error("--verify-only requires --archive-dir "
                             "(a receipt alone cannot prove archive identity)")
            report = verify_receipt(lock, args.staged_dir, args.receipt,
                                    args.archive_dir, patch_map=patch_map)
            print(f"verified {len(report)} staged sources against {args.receipt}")
            return 0
    except SourceVerificationError as exc:
        print(f"SOURCE IDENTITY FAILURE: {exc}", file=sys.stderr)
        return 1
    parser.error("nothing to do: pass --stage-out, --verify-only, or --compare-tree")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

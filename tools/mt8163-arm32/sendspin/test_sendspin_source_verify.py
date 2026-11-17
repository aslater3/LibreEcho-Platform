#!/usr/bin/env python3
"""Unit tests for the byte-level Sendspin source identity verifier.

Dependency-free and hermetic: synthetic archives are built with ``tarfile`` so the
tests run in the default suite without any heavy SDK inputs.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_sendspin_sources as v  # noqa: E402

C1 = "1" * 40
C2 = "2" * 40
SUB = "3" * 40


def make_archive(directory: Path, name: str, commit: str, files: dict[str, bytes]) -> Path:
    top = f"{name}-{commit}"
    archive = directory / f"{top}.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        for rel, body in files.items():
            payload = body
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(payload)
            handle.addfile(info, io.BytesIO(payload))
    return archive


def frozen_patch(path: Path) -> "v.VerifiedPatch":
    """Read a test patch once and hand it to the verifier as frozen bytes."""
    data = path.read_bytes()
    return v.VerifiedPatch(path.name, path, hashlib.sha256(data).hexdigest(), data)


def build_lock(archive_dir: Path) -> dict:
    import hashlib
    a = make_archive(archive_dir, "libA", C1, {"a.txt": b"alpha"})
    b = make_archive(archive_dir, "libB", C2, {"b.txt": b"beta"})
    sub = make_archive(archive_dir, "sub", SUB, {"s.txt": b"sub"})
    lock = {
        "name": "fixture",
        "dependencies": [
            {"name": "libA", "commit": C1,
             "archive_sha256": hashlib.sha256(a.read_bytes()).hexdigest(),
             "archive_url": f"https://x/libA/{C1}.tar.gz"},
            {"name": "libB", "commit": C2,
             "archive_sha256": hashlib.sha256(b.read_bytes()).hexdigest(),
             "archive_url": f"https://x/libB/{C2}.tar.gz",
             "submodules": [{"path": "lib/sub", "commit": SUB,
                             "archive_sha256": hashlib.sha256(sub.read_bytes()).hexdigest(),
                             "archive_url": f"https://x/sub/{SUB}.tar.gz"}]},
        ],
    }
    return lock


class SourceVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archives = self.root / "archives"
        self.archives.mkdir()
        self.lock = build_lock(self.archives)
        self.lock_path = self.root / "SOURCE.lock"
        self.lock_path.write_text(json.dumps(self.lock), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _lock(self) -> dict:
        return v.load_lock(self.lock_path)

    def test_stage_then_verify_roundtrip(self) -> None:
        lock = self._lock()
        out = self.root / "staged"
        receipt = self.root / "receipt.json"
        result = v.stage(lock, self.archives, out, receipt)
        self.assertTrue(result["complete"])
        self.assertEqual(set(result["entries"]), {"libA", "libB", "libB::lib/sub"})
        self.assertEqual((out / "libA" / "a.txt").read_bytes(), b"alpha")
        self.assertEqual((out / "libB" / "lib" / "sub" / "s.txt").read_bytes(), b"sub")
        report = v.verify_receipt(lock, out, receipt, self.archives)
        self.assertEqual(set(report), {"libA", "libB", "libB::lib/sub"})

    def test_archive_byte_tamper_is_rejected(self) -> None:
        # A staged tree named after the right commit but with different bytes must
        # fail: identity is the archive hash, not the file name.
        bad = self.archives / f"libA-{C1}.tar.gz"
        bad.write_bytes(bad.read_bytes() + b"tamper")
        with self.assertRaises(v.SourceVerificationError):
            v.stage(self._lock(), self.archives, self.root / "staged2", self.root / "r.json")

    def test_name_only_identity_is_rejected(self) -> None:
        # An archive whose name embeds the commit but whose bytes do not match the
        # lock digest must be rejected.
        broken = self.root / "nameonly"
        broken.mkdir()
        make_archive(broken, "libA", C1, {"a.txt": b"WRONG"})
        make_archive(broken, "libB", C2, {"b.txt": b"beta"})
        make_archive(broken, "sub", SUB, {"s.txt": b"sub"})
        with self.assertRaises(v.SourceVerificationError):
            v.stage(self._lock(), broken, self.root / "staged3", self.root / "r3.json")

    def test_receipt_declared_digest_without_matching_files_is_rejected(self) -> None:
        lock = self._lock()
        out = self.root / "staged4"
        receipt = self.root / "receipt4.json"
        v.stage(lock, self.archives, out, receipt)
        # Tamper a staged file after the receipt was written.
        (out / "libA" / "a.txt").write_bytes(b"tampered")
        with self.assertRaises(v.SourceVerificationError):
            v.verify_receipt(lock, out, receipt, self.archives)

    def test_receipt_with_forged_tree_digest_is_rejected(self) -> None:
        lock = self._lock()
        out = self.root / "staged5"
        receipt = self.root / "receipt5.json"
        v.stage(lock, self.archives, out, receipt)
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["entries"]["libA"]["tree_digest"] = "f" * 64
        receipt.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(v.SourceVerificationError):
            v.verify_receipt(lock, out, receipt, self.archives)

    def test_incomplete_receipt_closure_is_rejected(self) -> None:
        lock = self._lock()
        out = self.root / "staged6"
        receipt = self.root / "receipt6.json"
        v.stage(lock, self.archives, out, receipt)
        data = json.loads(receipt.read_text(encoding="utf-8"))
        del data["entries"]["libB::lib/sub"]
        receipt.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(v.SourceVerificationError):
            v.verify_receipt(lock, out, receipt, self.archives)

    def test_missing_archive_is_rejected(self) -> None:
        missing = self.root / "empty"
        missing.mkdir()
        with self.assertRaises(v.SourceVerificationError):
            v.stage(self._lock(), missing, self.root / "staged7", self.root / "r7.json")

    def test_lock_without_digest_is_rejected(self) -> None:
        bad = self.root / "badlock.json"
        bad.write_text(json.dumps({"dependencies": [{"name": "x", "commit": C1}]}), encoding="utf-8")
        with self.assertRaises(v.SourceVerificationError):
            v.load_lock(bad)

    def test_compare_tree_is_name_agnostic_and_byte_exact(self) -> None:
        lock = self._lock()
        staged = self.root / "staged_cmp"
        receipt = self.root / "rc.json"
        v.stage(lock, self.archives, staged, receipt)
        # Rename exactly like a Product stage would; identity must come from bytes.
        (staged / "libA").rename(staged / "sendspin-liba")
        (staged / "libB").rename(staged / "sendspin-libb")
        work = self.root / "cmpwork"
        work.mkdir()
        report = v.compare_staged_tree(lock, self.archives, staged, work)
        self.assertEqual(set(report), {"libA", "libB", "libB::lib/sub"})

    def test_compare_tree_rejects_tampered_copy(self) -> None:
        lock = self._lock()
        staged = self.root / "staged_cmp2"
        v.stage(lock, self.archives, staged, self.root / "rc2.json")
        (staged / "libA" / "a.txt").write_bytes(b"tampered")
        work = self.root / "cmpwork2"
        work.mkdir()
        with self.assertRaises(v.SourceVerificationError):
            v.compare_staged_tree(lock, self.archives, staged, work)

    def test_compare_tree_rejects_missing_submodule(self) -> None:
        lock = self._lock()
        staged = self.root / "staged_cmp3"
        v.stage(lock, self.archives, staged, self.root / "rc3.json")
        import shutil
        shutil.rmtree(staged / "libB" / "lib" / "sub")
        work = self.root / "cmpwork3"
        work.mkdir()
        with self.assertRaises(v.SourceVerificationError):
            v.compare_staged_tree(lock, self.archives, staged, work)

    # --- verify_receipt must root identity in the pinned archive, not the receipt.

    def _reforge(self, receipt: Path, name: str, staged_tree: Path,
                 exclude: tuple[str, ...] = ()) -> None:
        """Rewrite the receipt so it *consistently* declares the on-disk tree.

        This is exactly the trust-boundary forgery: after tampering the staged
        bytes, the attacker recomputes tree_digest/file_count so they still match
        the receipt. A verifier that trusts the declared digest accepts it.
        """
        digest, count = v.tree_digest(staged_tree, exclude)
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["entries"][name]["tree_digest"] = digest
        data["entries"][name]["file_count"] = count
        receipt.write_text(json.dumps(data), encoding="utf-8")

    def test_consistent_forged_receipt_is_rejected(self) -> None:
        # Tamper the tree *and* reforge the receipt to match it: archive-dir is
        # present, so the verifier must re-derive the pinned tree and reject.
        lock = self._lock()
        out = self.root / "staged_forge"
        receipt = self.root / "receipt_forge.json"
        v.stage(lock, self.archives, out, receipt)
        (out / "libA" / "a.txt").write_bytes(b"EVIL")
        self._reforge(receipt, "libA", out / "libA")
        with self.assertRaises(v.SourceVerificationError):
            v.verify_receipt(lock, out, receipt, self.archives)

    def test_consistent_forged_submodule_receipt_is_rejected(self) -> None:
        # The submodule-aware parent contract: a submodule entry whose bytes are
        # tampered must be caught even when its own declared digest is forged.
        lock = self._lock()
        out = self.root / "staged_forge_sub"
        receipt = self.root / "receipt_forge_sub.json"
        v.stage(lock, self.archives, out, receipt)
        subdir = out / "libB" / "lib" / "sub"
        (subdir / "s.txt").write_bytes(b"EVIL")
        self._reforge(receipt, "libB::lib/sub", subdir)
        with self.assertRaises(v.SourceVerificationError):
            v.verify_receipt(lock, out, receipt, self.archives)

    def test_verify_only_requires_archive_dir(self) -> None:
        # A receipt cannot prove archive identity from its own declared values.
        lock = self._lock()
        out = self.root / "staged_req"
        receipt = self.root / "receipt_req.json"
        v.stage(lock, self.archives, out, receipt)
        with self.assertRaises(v.SourceVerificationError):
            v.verify_receipt(lock, out, receipt)

    def test_verify_receipt_rejects_missing_archives(self) -> None:
        lock = self._lock()
        out = self.root / "staged_miss"
        receipt = self.root / "receipt_miss.json"
        v.stage(lock, self.archives, out, receipt)
        empty = self.root / "empty_archives"
        empty.mkdir()
        with self.assertRaises(v.SourceVerificationError):
            v.verify_receipt(lock, out, receipt, empty)

    def test_receipt_archive_name_mapping_cannot_redirect(self) -> None:
        # A receipt that remaps an entry onto another *real*, hash-valid pinned
        # archive must be refused — the lock commit decides which archive is read.
        lock = self._lock()
        out = self.root / "staged_map"
        receipt = self.root / "receipt_map.json"
        v.stage(lock, self.archives, out, receipt)
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["entries"]["libA"]["archive_file"] = f"libB-{C2}.tar.gz"
        receipt.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(v.SourceVerificationError):
            v.verify_receipt(lock, out, receipt, self.archives)

    def test_receipt_archive_file_traversal_is_rejected(self) -> None:
        # A receipt-supplied archive_file containing a traversal must never be
        # followed; the real archive is found from the lock commit.
        lock = self._lock()
        out = self.root / "staged_trav"
        receipt = self.root / "receipt_trav.json"
        v.stage(lock, self.archives, out, receipt)
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["entries"]["libA"]["archive_file"] = "../../etc/passwd"
        receipt.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(v.SourceVerificationError):
            v.verify_receipt(lock, out, receipt, self.archives)

    def test_receipt_staged_path_escape_is_rejected(self) -> None:
        # A lock path that escapes the staged root must be refused explicitly,
        # not read as an empty/absent tree.
        lock = self._lock()
        out = self.root / "staged_esc"
        receipt = self.root / "receipt_esc.json"
        v.stage(lock, self.archives, out, receipt)
        lock["libA"]["path"] = "../libA"
        with self.assertRaises(v.SourceVerificationError) as ctx:
            v.verify_receipt(lock, out, receipt, self.archives)
        self.assertIn("escape", str(ctx.exception))

    # ------------------------------------------------------------------
    # Input races: digest verification and byte consumption must observe the
    # SAME bytes.  Verification-to-use windows are injected deterministically
    # by mutating the input inside the first _extract call (the exact window a
    # concurrent writer would exploit); the verified bytes must still win.
    # ------------------------------------------------------------------

    def _race_first_extraction(self, mutation) -> None:
        """Mutate the pinned input at the first extraction call, once.

        This fires after the archive digest has been checked and before the
        bytes are consumed: the reviewer TOCTOU window, made deterministic.
        """
        real = v._extract
        state = {"fired": False}

        def racing(source, destination, _real=real, _state=state, _mutation=mutation):
            if not _state["fired"]:
                _state["fired"] = True
                _mutation()
            return _real(source, destination)

        v._extract = racing
        self.addCleanup(setattr, v, "_extract", real)

    def test_symlinked_archive_is_rejected(self) -> None:
        real = self.archives / f"libA-{C1}.tar.gz"
        real.rename(self.archives / "relocated-libA-archive.tar.gz")
        os.symlink("relocated-libA-archive.tar.gz", self.archives / f"libA-{C1}.tar.gz")
        with self.assertRaises(v.SourceVerificationError):
            v.stage(self._lock(), self.archives, self.root / "staged_symlink_archive",
                    self.root / "receipt_symlink_archive.json")

    def test_archive_in_place_mutation_after_verify_cannot_substitute_bytes(self) -> None:
        evil = archive_bytes("libA", C1, {"a.txt": b"EVIL-ARCHIVE"})
        archive = self.archives / f"libA-{C1}.tar.gz"

        def mutate():
            with open(archive, "r+b") as handle:
                handle.write(evil)
                handle.truncate()

        self._race_first_extraction(mutate)
        out = self.root / "staged_inplace"
        v.stage(self._lock(), self.archives, out, self.root / "r_inplace.json")
        self.assertEqual((out / "libA" / "a.txt").read_bytes(), b"alpha")

    def test_archive_symlink_swap_after_verify_cannot_substitute_bytes(self) -> None:
        evil = self.root / "evil-libA-archive.tar.gz"
        evil.write_bytes(archive_bytes("libA", C1, {"a.txt": b"EVIL-ARCHIVE"}))
        archive = self.archives / f"libA-{C1}.tar.gz"

        def mutate():
            archive.unlink()
            os.symlink(str(evil), archive)

        self._race_first_extraction(mutate)
        out = self.root / "staged_swap"
        v.stage(self._lock(), self.archives, out, self.root / "r_swap.json")
        self.assertEqual((out / "libA" / "a.txt").read_bytes(), b"alpha")

    def test_verify_receipt_cannot_be_fooled_by_forgery_plus_archive_swap(self) -> None:
        # The strongest forgery: the staged bytes are tampered AND the receipt
        # is reforged to match them, AND the archive path is swapped between its
        # digest check and the derivation of the pinned tree.  Identity must
        # still be re-derived from the bytes that were verified.
        lock = self._lock()
        out = self.root / "staged_v2"
        receipt = self.root / "receipt_v2.json"
        v.stage(lock, self.archives, out, receipt)
        (out / "libA" / "a.txt").write_bytes(b"EVIL-ARCHIVE")
        self._reforge(receipt, "libA", out / "libA")
        archive = self.archives / f"libA-{C1}.tar.gz"
        evil = archive_bytes("libA", C1, {"a.txt": b"EVIL-ARCHIVE"})
        self._race_first_extraction(lambda: archive.write_bytes(evil))
        with self.assertRaises(v.SourceVerificationError):
            v.verify_receipt(lock, out, receipt, self.archives)

    def test_compare_tree_cannot_be_fooled_by_archive_swap_during_derivation(self) -> None:
        lock = self._lock()
        staged = self.root / "staged_c1"
        v.stage(lock, self.archives, staged, self.root / "r_c1.json")
        (staged / "libA" / "a.txt").write_bytes(b"EVIL-ARCHIVE")
        archive = self.archives / f"libA-{C1}.tar.gz"
        evil = archive_bytes("libA", C1, {"a.txt": b"EVIL-ARCHIVE"})
        self._race_first_extraction(lambda: archive.write_bytes(evil))
        work = self.root / "work_c1"
        work.mkdir()
        with self.assertRaises(v.SourceVerificationError):
            v.compare_staged_tree(lock, self.archives, staged, work)

    def test_symlinked_staged_path_is_rejected(self) -> None:
        lock = self._lock()
        out = self.root / "staged_v1"
        receipt = self.root / "receipt_v1.json"
        v.stage(lock, self.archives, out, receipt)
        (out / "libA").rename(out / "libA-real")
        os.symlink("libA-real", out / "libA")
        with self.assertRaises(v.SourceVerificationError):
            v.verify_receipt(lock, out, receipt, self.archives)

    def test_compare_tree_does_not_follow_symlinked_candidates(self) -> None:
        lock = self._lock()
        staged = self.root / "staged_c2"
        v.stage(lock, self.archives, staged, self.root / "r_c2.json")
        outside = self.root / "outside_c2"
        outside.mkdir()
        (staged / "libA").rename(outside / "libA")
        os.symlink(str(outside / "libA"), staged / "libA")
        work = self.root / "work_c2"
        work.mkdir()
        with self.assertRaises(v.SourceVerificationError):
            v.compare_staged_tree(lock, self.archives, staged, work)

    def test_archive_planting_a_symlinked_directory_is_refused_before_publication(self) -> None:
        # tarfile's data filter refuses escaping link bodies, but an in-tree
        # link to a directory still lands in the transformed tree.  A tree
        # that would be published must not contain symlinked directories.
        infra = self.root / "dirlink_archives"
        infra.mkdir()
        archive = make_symlink_archive(infra, "libA", C1, {"alias": "sub"},
                                       {"sub/f.txt": b"x"})
        lock_data = {"dependencies": [{
            "name": "libA", "commit": C1,
            "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "archive_url": f"https://x/libA/{C1}.tar.gz"}]}
        lock_path = self.root / "dirlink.lock"
        lock_path.write_text(json.dumps(lock_data), encoding="utf-8")
        with self.assertRaises(v.SourceVerificationError):
            v.stage(v.load_lock(lock_path), infra, self.root / "staged_dirlink",
                    self.root / "r_dirlink.json")

    def test_stage_refuses_existing_output_root(self) -> None:
        out = self.root / "stale_out"
        out.mkdir()
        (out / "junk.txt").write_bytes(b"stale")
        with self.assertRaises(v.SourceVerificationError):
            v.stage(self._lock(), self.archives, out, self.root / "r_stale.json")
        self.assertEqual((out / "junk.txt").read_bytes(), b"stale")

    def test_failed_stage_publishes_no_receipt_and_refuses_reuse(self) -> None:
        lock = self._lock()
        bad = self.archives / f"libB-{C2}.tar.gz"
        bad.write_bytes(bad.read_bytes() + b"tamper")
        out = self.root / "staged_failed"
        receipt = self.root / "receipt_failed.json"
        with self.assertRaises(v.SourceVerificationError):
            v.stage(lock, self.archives, out, receipt)
        self.assertFalse(receipt.exists())
        with self.assertRaises(v.SourceVerificationError):
            v.stage(lock, self.archives, out, receipt)

    def test_verify_receipt_refuses_a_stale_workdir(self) -> None:
        lock = self._lock()
        out = self.root / "staged_w"
        receipt = self.root / "receipt_w.json"
        v.stage(lock, self.archives, out, receipt)
        work = self.root / "work_w"
        (work / "exp_libA").mkdir(parents=True)
        with self.assertRaises(v.SourceVerificationError):
            v.verify_receipt(lock, out, receipt, self.archives, workdir=work)


# ============================================================================
# patch_inventory: a closed, SHA-256-pinned, single-apply transform
# ============================================================================
#
# SOURCE.lock may declare patches applied to an archive-verified staged tree. These
# tests pin the contract: the transform is deterministic, the inventory is closed
# (missing/extra/corrupt entries refused), the diff may not escape the tree, and a
# pristine or double-applied tree is refused against a declared inventory.

ALPHA_PATCH = (b"--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-alpha\n\\ No newline at end of file\n"
               b"+ALPHA\n\\ No newline at end of file\n")
# A second patch whose context is the ALPHA_PATCH result, so the pair is order-sensitive:
# it applies only after 0001 and a reversed inventory is refused.
BETA_PATCH = (b"--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-ALPHA\n\\ No newline at end of file\n"
              b"+ALPHABETA\n\\ No newline at end of file\n")


class PatchInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archives = self.root / "archives"
        self.archives.mkdir()
        self.lock = build_lock(self.archives)
        self.lock_path = self.root / "SOURCE.lock"
        self.lock_path.write_text(json.dumps(self.lock), encoding="utf-8")
        self.pdir = self.root / "patches"
        self.pdir.mkdir()
        self.patch_name = "0001-alpha.patch"
        (self.pdir / self.patch_name).write_bytes(ALPHA_PATCH)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _declare(self, records: list[dict]) -> None:
        data = json.loads(self.lock_path.read_text(encoding="utf-8"))
        data["patch_inventory"] = {"schema": v.PATCH_SCHEMA, "applied": records}
        self.lock_path.write_text(json.dumps(data), encoding="utf-8")

    def _record(self, name: str | None = None, digest: str | None = None,
                target: str = "libA") -> dict:
        name = name or self.patch_name
        if digest is None:
            digest = hashlib.sha256((self.pdir / name).read_bytes()).hexdigest()
        return {"file": name, "sha256": digest, "target": target}

    def _map(self) -> dict:
        return v.load_patch_inventory(v.load_lock(self.lock_path), self.lock_path, self.pdir)

    def test_declared_patch_transforms_staged_and_compared_tree(self) -> None:
        self._declare([self._record()])
        lock = v.load_lock(self.lock_path)
        patch_map = self._map()
        out = self.root / "staged"
        receipt = self.root / "r.json"
        v.stage(lock, self.archives, out, receipt, patch_map)
        self.assertEqual((out / "libA" / "a.txt").read_bytes(), b"ALPHA")
        # Untouched sources are still pristine.
        self.assertEqual((out / "libB" / "b.txt").read_bytes(), b"beta")
        v.verify_receipt(lock, out, receipt, self.archives, patch_map=patch_map)
        work = self.root / "w"
        work.mkdir()
        v.compare_staged_tree(lock, self.archives, out, work, patch_map)

    def test_pristine_tree_is_rejected_once_a_patch_is_declared(self) -> None:
        self._declare([self._record()])
        lock = v.load_lock(self.lock_path)
        # A staged tree that is the unmodified upstream archive (no patch applied).
        pristine = self.root / "pristine"
        v.stage(lock, self.archives, pristine, self.root / "p.json")
        work = self.root / "w2"
        work.mkdir()
        with self.assertRaises(v.SourceVerificationError):
            v.compare_staged_tree(lock, self.archives, pristine, work, self._map())

    def test_missing_patch_file_is_rejected(self) -> None:
        # Declared on disk is impossible here; the missing-file check fires before the digest.
        self._declare([{"file": "absent.patch", "sha256": "0" * 64, "target": "libA"}])
        with self.assertRaises(v.SourceVerificationError):
            self._map()

    def test_extra_patch_file_is_rejected(self) -> None:
        (self.pdir / "0002-undeclared.patch").write_bytes(ALPHA_PATCH)
        self._declare([self._record()])
        with self.assertRaises(v.SourceVerificationError):
            self._map()

    def test_corrupt_patch_digest_is_rejected(self) -> None:
        self._declare([self._record(digest="0" * 64)])
        with self.assertRaises(v.SourceVerificationError):
            self._map()

    def test_patch_file_with_a_path_component_is_rejected(self) -> None:
        self._declare([{"file": "sub/0001-alpha.patch", "sha256": "0" * 64, "target": "libA"}])
        with self.assertRaises(v.SourceVerificationError):
            self._map()

    def test_pristine_archive_anchor_mismatch_is_rejected(self) -> None:
        record = self._record()
        record["pristine_archive_sha256"] = "0" * 64
        self._declare([record])
        with self.assertRaises(v.SourceVerificationError):
            self._map()

    def test_unsafe_patch_target_is_rejected(self) -> None:
        evil = self.pdir / "evil.patch"
        evil.write_bytes(b"--- a/../escape.txt\n+++ b/../escape.txt\n@@ -1 +1 @@\n-x\n+y\n")
        tree = self.root / "etree"
        tree.mkdir()
        with self.assertRaises(v.SourceVerificationError):
            v.apply_patches(tree, [frozen_patch(evil)])

    def test_apply_patches_refuses_a_second_application(self) -> None:
        tree = self.root / "dtree"
        tree.mkdir()
        (tree / "a.txt").write_bytes(b"alpha")
        v.apply_patches(tree, [frozen_patch(self.pdir / self.patch_name)])
        self.assertEqual((tree / "a.txt").read_bytes(), b"ALPHA")
        with self.assertRaises(v.SourceVerificationError):
            v.apply_patches(tree, [frozen_patch(self.pdir / self.patch_name)])

    def test_staged_tree_cannot_be_patch_applied_again(self) -> None:
        # The staged tree is already the single-applied transform; re-applying the same
        # patch to it must be refused (the single-apply guard), so a double-applied tree
        # can never be produced or accepted.
        self._declare([self._record()])
        lock = v.load_lock(self.lock_path)
        patch_map = self._map()
        staged = self.root / "staged_double"
        v.stage(lock, self.archives, staged, self.root / "d.json", patch_map)
        self.assertEqual((staged / "libA" / "a.txt").read_bytes(), b"ALPHA")
        with self.assertRaises(v.SourceVerificationError):
            v.apply_patches(staged / "libA", [frozen_patch(self.pdir / self.patch_name)])

    # ------------------------------------------------------------------
    # Ordered multi-patch inventory (0001 then 0002): the transform is the
    # patches applied in *list order*, and the order is part of identity.
    # ------------------------------------------------------------------

    def _declare_pair(self, first: str = "0001-alpha.patch",
                      second: str = "0002-beta.patch") -> None:
        (self.pdir / "0002-beta.patch").write_bytes(BETA_PATCH)
        self._declare([self._record(first), self._record(second)])

    def test_ordered_patches_transform_in_list_order(self) -> None:
        self._declare_pair()
        lock = v.load_lock(self.lock_path)
        patch_map = self._map()
        out = self.root / "staged_order"
        receipt = self.root / "order.json"
        v.stage(lock, self.archives, out, receipt, patch_map)
        self.assertEqual((out / "libA" / "a.txt").read_bytes(), b"ALPHABETA")
        # Both identity derivations agree with the ordered transform.
        v.verify_receipt(lock, out, receipt, self.archives, patch_map=patch_map)
        work = self.root / "ow"
        work.mkdir()
        v.compare_staged_tree(lock, self.archives, out, work, patch_map)

    def test_reversed_patch_order_is_refused(self) -> None:
        # 0002's context is 0001's result, so an inventory that lists it first cannot
        # apply and the ordered transform is refused rather than silently producing a
        # different tree.
        self._declare_pair(first="0002-beta.patch", second="0001-alpha.patch")
        with self.assertRaises(v.SourceVerificationError):
            v.stage(v.load_lock(self.lock_path), self.archives, self.root / "staged_rev",
                    self.root / "rev.json", self._map())

    def test_second_patch_digest_tamper_is_rejected(self) -> None:
        self._declare_pair()
        declared = json.loads(self.lock_path.read_text(encoding="utf-8"))
        declared["patch_inventory"]["applied"][1]["sha256"] = "0" * 64
        self.lock_path.write_text(json.dumps(declared), encoding="utf-8")
        with self.assertRaises(v.SourceVerificationError):
            self._map()

    def test_second_patch_anchor_mismatch_is_rejected(self) -> None:
        self._declare_pair()
        declared = json.loads(self.lock_path.read_text(encoding="utf-8"))
        declared["patch_inventory"]["applied"][1]["pristine_archive_sha256"] = "0" * 64
        self.lock_path.write_text(json.dumps(declared), encoding="utf-8")
        with self.assertRaises(v.SourceVerificationError):
            self._map()

    # ------------------------------------------------------------------
    # A declared patch is a digest-pinned input: it is opened once with no
    # symlink following, its verified bytes are the only bytes parsed and fed
    # to `patch`, and the directory is enumerated closed.
    # ------------------------------------------------------------------

    def test_symlinked_patch_is_rejected(self) -> None:
        benign = self.root / "symlink-target.patch"
        benign.write_bytes(ALPHA_PATCH)
        link = self.pdir / self.patch_name
        link.unlink()
        os.symlink(str(benign), link)
        self._declare([self._record()])
        with self.assertRaises(v.SourceVerificationError):
            self._map()

    def test_patch_in_place_mutation_after_load_cannot_change_applied_bytes(self) -> None:
        self._declare([self._record()])
        lock = v.load_lock(self.lock_path)
        patch_map = self._map()
        with open(self.pdir / self.patch_name, "r+b") as handle:
            handle.write(MALICIOUS_PATCH)
            handle.truncate()
        out = self.root / "staged_patch_race"
        v.stage(lock, self.archives, out, self.root / "pr.json", patch_map)
        self.assertEqual((out / "libA" / "a.txt").read_bytes(), b"ALPHA")

    def test_patch_symlink_swap_after_load_cannot_change_applied_bytes(self) -> None:
        self._declare([self._record()])
        lock = v.load_lock(self.lock_path)
        patch_map = self._map()
        evil = self.root / "evil-swap.patch"
        evil.write_bytes(MALICIOUS_PATCH)
        link = self.pdir / self.patch_name
        link.unlink()
        os.symlink(str(evil), link)
        out = self.root / "staged_patch_swap"
        v.stage(lock, self.archives, out, self.root / "ps.json", patch_map)
        self.assertEqual((out / "libA" / "a.txt").read_bytes(), b"ALPHA")

    def test_dotfile_entry_in_patch_dir_is_rejected(self) -> None:
        self._declare([self._record()])
        (self.pdir / ".hidden.patch").write_bytes(ALPHA_PATCH)
        with self.assertRaises(v.SourceVerificationError):
            self._map()

    def test_subdirectory_entry_in_patch_dir_is_rejected(self) -> None:
        self._declare([self._record()])
        (self.pdir / "subdir").mkdir()
        with self.assertRaises(v.SourceVerificationError):
            self._map()

    def test_undeclared_nonpatch_entry_in_patch_dir_is_rejected(self) -> None:
        self._declare([self._record()])
        (self.pdir / "notes.txt").write_bytes(b"not a patch")
        with self.assertRaises(v.SourceVerificationError):
            self._map()

    def test_directory_named_patch_is_rejected(self) -> None:
        (self.pdir / "0002-dir.patch").mkdir()
        self._declare([self._record(),
                       {"file": "0002-dir.patch", "sha256": "0" * 64, "target": "libA"}])
        with self.assertRaises(v.SourceVerificationError):
            self._map()

    def test_patch_planting_an_escaping_symlink_is_refused(self) -> None:
        # GNU patch materializes git-style 120000 diffs, so a pinned patch must
        # never be able to publish a link that escapes the tree.
        (self.pdir / self.patch_name).write_bytes(SYMLINK_PATCH)
        self._declare([self._record(
            digest=hashlib.sha256(SYMLINK_PATCH).hexdigest())])
        lock = v.load_lock(self.lock_path)
        patch_map = self._map()
        with self.assertRaises(v.SourceVerificationError):
            v.stage(lock, self.archives, self.root / "staged_link",
                    self.root / "link.json", patch_map)

    def test_relative_patch_dir_cli_is_resolved_from_the_invocation_cwd(self) -> None:
        self._declare([self._record()])
        proc = subprocess.run(
            [sys.executable, str(Path(v.__file__).resolve()),
             "--lock", "SOURCE.lock", "--archive-dir", "archives",
             "--patch-dir", "patches", "--stage-out", "staged_cli",
             "--receipt", "receipt_cli.json"],
            cwd=self.root, capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((self.root / "staged_cli" / "libA" / "a.txt").read_bytes(), b"ALPHA")


MALICIOUS_PATCH = (b"--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-alpha\n\\ No newline at end of file\n"
                   b"+PWNED-BY-TOCTOU\n\\ No newline at end of file\n")

# A git-style symlink creation diff: GNU patch materializes the link body
# verbatim, so a digest-pinned patch can plant a symlink in the transformed tree.
SYMLINK_PATCH = (b"diff --git a/evil-link b/evil-link\nnew file mode 120000\n"
                 b"--- /dev/null\n+++ b/evil-link\n@@ -0,0 +1 @@\n+/etc/passwd\n"
                 b"\\ No newline at end of file\n")


def archive_bytes(name: str, commit: str, files: dict[str, bytes]) -> bytes:
    top = f"{name}-{commit}"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as handle:
        for rel, body in files.items():
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(body)
            handle.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def make_symlink_archive(directory: Path, name: str, commit: str,
                         links: dict[str, str],
                         files: dict[str, bytes] | None = None) -> Path:
    top = f"{name}-{commit}"
    archive = directory / f"{top}.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        for rel, body in (files or {}).items():
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(body)
            handle.addfile(info, io.BytesIO(body))
        for rel, target in links.items():
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.type = tarfile.SYMTYPE
            info.linkname = target
            handle.addfile(info)
    return archive


class TreeConfinementTests(unittest.TestCase):
    """A transformed tree must be safe before it is published or accepted."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "tree"
        self.root.mkdir()
        (self.root / "ok.txt").write_bytes(b"ok")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "nested.txt").write_bytes(b"nested")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_real_tree_shape_with_in_tree_symlinks_is_accepted(self) -> None:
        os.symlink("ok.txt", self.root / "alias.txt")
        os.symlink("../ok.txt", self.root / "sub" / "up.txt")
        v._assert_tree_confined(self.root)

    def test_escaping_relative_symlink_is_rejected(self) -> None:
        os.symlink("../../outside.txt", self.root / "sub" / "esc.txt")
        with self.assertRaises(v.SourceVerificationError):
            v._assert_tree_confined(self.root)

    def test_absolute_symlink_is_rejected(self) -> None:
        os.symlink("/etc/passwd", self.root / "abs.txt")
        with self.assertRaises(v.SourceVerificationError):
            v._assert_tree_confined(self.root)

    def test_symlink_to_directory_is_rejected(self) -> None:
        os.symlink("sub", self.root / "dirlink")
        with self.assertRaises(v.SourceVerificationError):
            v._assert_tree_confined(self.root)

    def test_setuid_file_is_rejected(self) -> None:
        (self.root / "suid").write_bytes(b"x")
        os.chmod(self.root / "suid", 0o4755)
        with self.assertRaises(v.SourceVerificationError):
            v._assert_tree_confined(self.root)

    def test_nonregular_entry_is_rejected(self) -> None:
        os.mkfifo(self.root / "pipe")
        with self.assertRaises(v.SourceVerificationError):
            v._assert_tree_confined(self.root)


if __name__ == "__main__":
    unittest.main()

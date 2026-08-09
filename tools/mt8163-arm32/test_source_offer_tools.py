#!/usr/bin/env python3
"""Tests for deterministic LibreEcho source-offer assembly."""
from __future__ import annotations

import importlib.util
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("assemble_source_offer.py")
spec = importlib.util.spec_from_file_location("assemble_source_offer", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("source-offer module cannot be loaded")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SourceOfferTests(unittest.TestCase):
    def test_archive_is_deterministic_and_manifest_covers_every_member(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.txt"
            source.write_text("source\n")
            relink = root / "adapter.o"
            relink.write_bytes(b"object")
            first = root / "first.tar.gz"
            second = root / "second.tar.gz"

            kwargs = dict(
                component="tts-payload",
                source_files=[(source, "sources/source.txt")],
                relink_files=[(relink, "relink/adapter.o")],
                metadata={"toolchain": "arm-linux-gnueabihf-gcc-13.3.0"},
                source_date_epoch=1_700_000_000,
            )
            manifest_a = module.assemble(output=first, **kwargs)
            manifest_b = module.assemble(output=second, **kwargs)

            self.assertEqual(module.sha256(first), module.sha256(second))
            self.assertEqual(manifest_a, manifest_b)
            self.assertEqual(manifest_a["component"], "tts-payload")
            self.assertEqual(
                [item["path"] for item in manifest_a["members"]],
                ["relink/adapter.o", "sources/source.txt"],
            )
            with tarfile.open(first, "r:gz") as archive:
                names = archive.getnames()
                manifest_file = archive.extractfile("SOURCE-OFFER-MANIFEST.json")
                self.assertIsNotNone(manifest_file)
                assert manifest_file is not None
                embedded = json.loads(manifest_file.read())
            self.assertEqual(
                names,
                ["SOURCE-OFFER-MANIFEST.json", "relink/adapter.o", "sources/source.txt"],
            )
            self.assertEqual(embedded, manifest_a)

    def test_unsafe_or_duplicate_logical_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            source.write_text("x")
            output = root / "offer.tar.gz"
            with self.assertRaisesRegex(ValueError, "unsafe logical path"):
                module.assemble(
                    component="bad", output=output,
                    source_files=[(source, "../escape")], relink_files=[],
                    metadata={}, source_date_epoch=1,
                )
            with self.assertRaisesRegex(ValueError, "duplicate logical path"):
                module.assemble(
                    component="bad", output=output,
                    source_files=[(source, "same")],
                    relink_files=[(source, "same")],
                    metadata={}, source_date_epoch=1,
                )

    def test_symlink_inputs_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            source.write_text("x")
            link = root / "link"
            link.symlink_to(source)
            with self.assertRaisesRegex(ValueError, "regular non-symlink"):
                module.assemble(
                    component="bad", output=root / "offer.tar.gz",
                    source_files=[(link, "source")], relink_files=[],
                    metadata={}, source_date_epoch=1,
                )

    def test_static_glibc_builders_preserve_relink_objects(self) -> None:
        tools = Path(__file__).resolve().parent
        expectations = {
            tools / "airplay/build_airplay.sh": (
                "LIBREECHO_AIRPLAY_RELINK_OUTPUT",
                "preserve_relink_objects",
            ),
            tools / "assistant/build_curl.sh": (
                "LIBREECHO_ASSISTANT_RELINK_OUTPUT",
                "preserve_relink_objects",
            ),
        }
        for path, markers in expectations.items():
            source = path.read_text()
            with self.subTest(path=path):
                for marker in markers:
                    self.assertIn(marker, source)
                self.assertIn("-name '*.o'", source)

    def test_glibc_builders_use_the_reviewed_sysroot_for_linking(self) -> None:
        tools = Path(__file__).resolve().parent
        for relative in (
            "airplay/build_airplay.sh",
            "assistant/build_curl.sh",
        ):
            source = (tools / relative).read_text(encoding="utf-8")
            with self.subTest(path=relative):
                self.assertIn('--sysroot=$SYSROOT', source)


if __name__ == "__main__":
    unittest.main()

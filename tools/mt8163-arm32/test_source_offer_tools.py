#!/usr/bin/env python3
"""Tests for deterministic LibreEcho source-offer assembly."""
from __future__ import annotations

import importlib.util
import hashlib
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
    def test_relink_names_are_content_addressed_and_host_independent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = b"same compiled object"
            digest = hashlib.sha256(payload).hexdigest()
            outputs = []
            manifests = []
            for host_path, output_name in (
                ("home/andy/workspace/onnxruntime-src", "home.tar.gz"),
                ("srv/runner/_work/onnxruntime-src", "runner.tar.gz"),
            ):
                object_file = root / host_path / "core" / "kernel.cc.o"
                object_file.parent.mkdir(parents=True)
                object_file.write_bytes(payload)
                output = root / output_name
                manifest = module.assemble(
                    component="stt-payload",
                    output=output,
                    source_files=[],
                    relink_files=[(
                        object_file,
                        f"onnxruntime-build/CMakeFiles/runtime.dir/{host_path}/core/kernel.cc.o",
                    )],
                    metadata={},
                    source_date_epoch=1_700_000_000,
                )
                outputs.append(output)
                manifests.append(manifest)

            expected = f"onnxruntime-build/objects/{digest}.o"
            self.assertEqual(module.sha256(outputs[0]), module.sha256(outputs[1]))
            self.assertEqual(manifests[0], manifests[1])
            self.assertEqual([item["path"] for item in manifests[0]["members"]], [expected])
            self.assertNotIn("/home/", json.dumps(manifests[0]))
            self.assertNotIn("/srv/", json.dumps(manifests[1]))

    def test_distinct_relink_bytes_are_preserved_and_identical_bytes_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = root / "host-a" / "same.o"
            duplicate = root / "host-b" / "renamed.o"
            distinct = root / "host-c" / "same.o"
            for path, payload in ((first, b"one"), (duplicate, b"one"), (distinct, b"two")):
                path.parent.mkdir(parents=True)
                path.write_bytes(payload)
            manifest = module.assemble(
                component="stt-payload",
                output=root / "offer.tar.gz",
                source_files=[],
                relink_files=[
                    (first, "runtime/CMakeFiles/a.dir/home/a/same.o"),
                    (duplicate, "runtime/CMakeFiles/b.dir/srv/b/renamed.o"),
                    (distinct, "runtime/CMakeFiles/c.dir/tmp/c/same.o"),
                ],
                metadata={},
                source_date_epoch=0,
            )
            expected = {
                f"runtime/objects/{hashlib.sha256(payload).hexdigest()}.o"
                for payload in (b"one", b"two")
            }
            self.assertEqual({item["path"] for item in manifest["members"]}, expected)
            self.assertEqual(len(manifest["members"]), 2)

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
            relink_path = (
                "relink/objects/"
                f"{hashlib.sha256(b'object').hexdigest()}.o"
            )
            self.assertEqual(
                [item["path"] for item in manifest_a["members"]],
                [relink_path, "sources/source.txt"],
            )
            with tarfile.open(first, "r:gz") as archive:
                names = archive.getnames()
                manifest_file = archive.extractfile("SOURCE-OFFER-MANIFEST.json")
                self.assertIsNotNone(manifest_file)
                assert manifest_file is not None
                embedded = json.loads(manifest_file.read())
            self.assertEqual(
                names,
                ["SOURCE-OFFER-MANIFEST.json", relink_path, "sources/source.txt"],
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
                    source_files=[(source, "same"), (source, "same")],
                    relink_files=[],
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

    def test_audio_tools_consume_exported_uapi_without_dirtying_kernel_source(self) -> None:
        source = (
            Path(__file__).resolve().parent / "audio-tools/build_audio_tools.sh"
        ).read_text(encoding="utf-8")
        self.assertNotIn("headers_install", source)
        self.assertIn("exported-linux-uapi", source)
        self.assertIn("kernel_uapi_sha256", source)


if __name__ == "__main__":
    unittest.main()

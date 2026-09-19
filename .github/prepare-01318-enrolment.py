#!/usr/bin/env python3
"""Exact-preimage source preparation; removed before the release PR is ready."""
import hashlib
import subprocess
from pathlib import Path

root = Path('tools/mt8163-arm32')
importer = root / 'initramfs/libreecho-vendor-import'
test = root / 'test_vendor_import_compat.py'
for path, expected in ((importer, 'cf9c16834a4f200731582c666a242091c2841190'), (test, '9de86f695481716e50b114aa555ab955ef43397a')):
    data = path.read_bytes()
    actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    assert actual == expected, f'preimage changed: {path}'

def replace_once(text, old, new):
    assert text.count(old) == 1, f'patch anchor not unique: {old!r}'
    return text.replace(old, new)

text = test.read_text()
text = replace_once(text, 'import os\n', 'import os\nimport shutil\n')
text = replace_once(text, 'from pathlib import Path\n', 'from pathlib import Path\nfrom unittest import mock\n')
text = replace_once(text, '            check=False,\n', '            check=False,\n            timeout=15,\n')
text = replace_once(text,
    '            second, enrolled_again, status_again = self.run_importer(root, source)\n',
    '''            # A cold boot discards all runtime bytes and status. Only the
            # persisted hash/size contract may authorise the second import.
            self.assertFalse(enrolled.with_name("vendor-import-force-next-boot").exists())
            self.assertEqual(sorted(p.name for p in enrolled.parent.iterdir()), ["vendor-assets.tsv"])
            self.assertFalse((root / "vendor-stage").exists())
            shutil.rmtree(root / "runtime-firmware")
            shutil.rmtree(root / "run")
            second, enrolled_again, status_again = self.run_importer(root, source)
''')
text = replace_once(text,
    '            self.assertIn("verification=owner-local-enrolled\\n", status_again.read_text())\n',
    '''            self.assertIn("verification=owner-local-enrolled\\n", status_again.read_text())
            self.assertFalse(enrolled.with_name("vendor-import-force-next-boot").exists())
            for name, payload in payloads.items():
                self.assertEqual((root / "runtime-firmware" / name).read_bytes(), payload)
            self.assertEqual((root / "runtime-firmware/WIFI_RAM_CODE").read_bytes(), payloads["WIFI_RAM_CODE_8163"])
''')
new_tests = r'''
    def test_damaged_enrolment_copy_is_rejected_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "system-a"
            write_payloads(source, unknown_payloads())
            bindir = root / "bin"
            bindir.mkdir()
            # Corrupt only the enrolled manifest copy, preserving its schema.
            # Schema validation alone must not bless different trust records.
            copier = bindir / "cp"
            copier.write_text(
                "#!/bin/sh\n"
                "/bin/cp \"$@\" || exit $?\n"
                "case \"$2\" in\n"
                "  */.vendor-assets.tsv.enrol.*)\n"
                "    sed '1s/^[^|]*/" + "0" * 64 + "/' \"$2\" > \"$2.damage\"\n"
                "    mv \"$2.damage\" \"$2\"\n"
                "    chmod 0600 \"$2\"\n"
                "    ;;\n"
                "esac\n"
            )
            copier.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"}):
                result, enrolled, status = self.run_importer(root, source, force=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("VENDOR_IMPORT_ENROLLED_SPEC_COPY_MISMATCH", result.stderr)
            self.assertFalse(enrolled.exists())
            self.assertIn("state=failed\n", status.read_text())
            self.assertEqual(list(enrolled.parent.iterdir()), [])

    def test_image_builder_pins_the_current_importer(self) -> None:
        expected = hashlib.sha256(IMPORTER.read_bytes()).hexdigest()
        self.assertIn(
            f'CONNECTIVITY_IMPORTER_SHA256 = "{expected}"',
            (TOOLS_DIR / "build_recovery_image.py").read_text(),
        )
'''
text = replace_once(text, '\n\nif __name__ == "__main__":\n', '\n' + new_tests + '\n\nif __name__ == "__main__":\n')
test.write_text(text)
result = subprocess.run(['python3', str(test), 'VendorImporterCompatTests.test_forced_unknown_set_enrols_hashes_then_verifies_without_force', 'VendorImporterCompatTests.test_damaged_enrolment_copy_is_rejected_before_commit', '-v'], text=True, capture_output=True, timeout=60)
print(result.stdout, result.stderr)
assert result.returncode == 1 and '0 != 2' in result.stderr and 'failures=1' in result.stderr, 'unexpected baseline result'
print('BASELINE_COLD_BOOT=PASS; DAMAGED_COPY_REGRESSION=FAIL_AS_EXPECTED')

text = importer.read_text()
old = '''    validate_spec_file "$enrolled_temp"
    mv "$enrolled_temp" "$ENROLLED_SPEC"
'''
new = '''    validate_spec_file "$enrolled_temp"
    # A valid schema is not proof that the stored trust record is the one
    # the owner accepted. Verify the exact copy before atomic publication.
    if ! cmp -s "$STAGE/ASSETS.tsv" "$enrolled_temp"; then
        rm -f "$enrolled_temp"
        fail VENDOR_IMPORT_ENROLLED_SPEC_COPY_MISMATCH
    fi
    sync || fail VENDOR_IMPORT_ENROLLED_SPEC_SYNC_FAILED
    mv "$enrolled_temp" "$ENROLLED_SPEC" || fail VENDOR_IMPORT_ENROLLED_SPEC_COMMIT_FAILED
    validate_enrolled_spec || fail VENDOR_IMPORT_ENROLLED_SPEC_INVALID
    cmp -s "$STAGE/ASSETS.tsv" "$ENROLLED_SPEC" || fail VENDOR_IMPORT_ENROLLED_SPEC_READBACK_MISMATCH
    sync || fail VENDOR_IMPORT_ENROLLED_SPEC_SYNC_FAILED
'''
text = replace_once(text, old, new)
importer.write_text(text)
old_digest = 'e9d98d059d7f0082d28bad134bf72fa6b6c4318a104d7de4001d9984df0e0854'
new_digest = hashlib.sha256(importer.read_bytes()).hexdigest()
builder = root / 'build_recovery_image.py'
builder.write_text(replace_once(builder.read_text(), f'CONNECTIVITY_IMPORTER_SHA256 = "{old_digest}"', f'CONNECTIVITY_IMPORTER_SHA256 = "{new_digest}"'))
readme = root / 'initramfs/vendor-assets/README.md'
readme.write_text(readme.read_text() + '''
## Enrolment durability and reboot regression (0.13.18)

Before publishing a new owner-local contract, the importer compares the temporary
manifest byte-for-byte with the verified staged contract and syncs it. It then
atomically renames the file, checks its type/mode/schema and exact contents again,
and syncs before reporting readiness. A failed copy, commit, readback or sync is
reported as an enrolment error rather than successful acceptance.

The regression test discards runtime firmware, transient staging and status
between two imports. With the force marker consumed, the second import must
recreate every runtime firmware file using only the unchanged mode-0600 enrolment
contract. A damaged-but-schema-valid manifest copy is rejected before commit.
These are synthetic host checks, not physical-device reboot acceptance.
''')
print('IMPORTER_SHA256=' + new_digest)

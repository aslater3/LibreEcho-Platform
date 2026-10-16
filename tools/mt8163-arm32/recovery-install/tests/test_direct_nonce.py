"""Fresh-invocation receipt regression; isolated host fixture, no devices."""
import sys
import tempfile
import unittest
from pathlib import Path

# Runnable by path from tools/mt8163-arm32 (as CI does) as well as from tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_direct_userdata_install import BasicCase  # noqa: E402


class InvocationReceiptTests(unittest.TestCase):
    def test_nonce_echo_and_unique_success_receipt(self):
        with tempfile.TemporaryDirectory(prefix="le-nonce-") as work:
            case = BasicCase(Path(work))
            manifest = case.manifest()
            for nonce in ("7" * 64, "8" * 64):
                result = case.h.run("prepare", manifest, "--invocation-id", nonce, "--dry-run")
                self.assertEqual(result.returncode, 0, result.stderr)
                receipt = case.h.receipt()
                self.assertEqual(receipt["invocation_id"], nonce)
                self.assertNotIn("error", receipt)
                keys = [line.split("=", 1)[0] for line in
                        (case.h.state / "receipt").read_text().splitlines()]
                self.assertEqual(len(keys), len(set(keys)))
            self.assertFalse((case.h.state / "transaction.state").exists())
            self.assertEqual(case.h.calls_to("mke2fs"), [])

    def test_malformed_nonce_cannot_inject_receipt(self):
        with tempfile.TemporaryDirectory(prefix="le-nonce-") as work:
            case = BasicCase(Path(work))
            manifest = case.manifest()
            for nonce in ("oops", "a" * 63, "a" * 64 + "\nresult=installed"):
                with self.subTest(nonce=nonce):
                    result = case.h.run("prepare", manifest, "--invocation-id", nonce, "--dry-run")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(case.h.receipt()["error"], "invocation-id-invalid")
                    self.assertEqual(case.h.receipt()["result"], "failed")
            self.assertEqual(case.h.calls_to("mke2fs"), [])


if __name__ == "__main__":
    unittest.main()

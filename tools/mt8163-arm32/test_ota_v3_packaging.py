"""Every v3 helper must ship in the verified image, not just in source."""
import unittest
from pathlib import Path
import build_recovery_image as build
import verify_recovery_image as verify
from test_multi_target import RadarTreeDiffTests

TOOLS = Path(__file__).resolve().parent
HELPERS = ('libreecho-target-manifest', 'libreecho-generation', 'libreecho-generation-transaction', 'libreecho-config-migrate')


class PackagingTests(unittest.TestCase):
    def test_all_helpers_are_staged_verified_and_allowlisted(self):
        source = Path(build.__file__).read_text()
        for helper in HELPERS:
            with self.subTest(helper=helper):
                self.assertIn('"' + helper + '":', source)
                self.assertEqual(verify.OVERLAY_FILES[helper], 0o755)
                self.assertEqual(verify.OVERLAY_TARGETS[helper], 'usr/local/sbin/' + helper)
                self.assertIn('usr/local/sbin/' + helper, RadarTreeDiffTests.ALLOWED)


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""Regression contracts for the 0.14 OTA rollback investigated in issue #169."""
from pathlib import Path
import json
import unittest

HERE = Path(__file__).resolve().parent
INIT = HERE / "initramfs/libreecho-init"
RUNTIME = HERE / "mdns/runtime-contract.json"


class Issue169HealthContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.init = INIT.read_text()
        self.runtime = json.loads(RUNTIME.read_text())

    def test_health_uses_host_visible_mdns_runtime(self) -> None:
        root = "/" + self.runtime["image_runtime_root"]
        dirs = self.runtime["runtime_dirs"]
        self.assertEqual(dirs["state_root"], root)
        self.assertEqual(dirs["bus"], root + "/run/dbus")
        self.assertEqual(dirs["services"], root + "/etc/avahi/services")
        self.assertIn(
            '[ -S "$MDNS_RUNTIME_ROOT/run/dbus/system_bus_socket" ] || return 1',
            self.init,
        )
        self.assertNotIn(
            "[ -S /run/libreecho/mdns/dbus/system_bus_socket ] || return 1",
            self.init,
        )

    def test_ota_attempts_start_only_after_startup_ready(self) -> None:
        wait = self.init.index("log ota-health-waiting-startup-ready")
        loop = self.init.index('while [ "$attempt" -lt 6 ] && [ "$passed" -lt 3 ]')
        self.assertLess(wait, loop)
        self.assertIn('while ! startup_ready_marker_valid && [ "$startup_wait" -lt 180 ]', self.init)
        self.assertIn("last_check=startup-ready", self.init)
        self.assertNotIn("# before making the slot permanent.\n    $BB sleep 45", self.init)

    def test_pstore_and_dynamic_pmsg_are_prepared(self) -> None:
        self.assertIn("setup_pstore()", self.init)
        self.assertIn('$BB mount -t pstore pstore "$pstore_dir"', self.init)
        self.assertIn("/sys/class/pmsg/pmsg0/dev", self.init)
        self.assertIn('$BB mknod /dev/pmsg0 c "$major" "$minor"', self.init)
        self.assertNotRegex(self.init, r"mknod /dev/pmsg0 c [0-9]+ [0-9]+")

    def test_pmsg_is_bounded_and_only_selected_markers_are_mirrored(self) -> None:
        self.assertIn("pmsg_marker()", self.init)
        self.assertIn('[ "${#marker}" -le 160 ] || return 0', self.init)
        self.assertIn("pmsg_marker mdns-health-not-ready", self.init)
        self.assertIn('pmsg_marker "ota-probe-failed-$last_check"', self.init)
        self.assertIn('pmsg_marker "ota-confirm-failed-$last_check"', self.init)
        # General log() output can contain board identity, URLs or configuration
        # and therefore must never be copied wholesale to pmsg.
        log_block = self.init[self.init.index("log()") : self.init.index("pmsg_marker()")]
        self.assertNotIn("pmsg", log_block)


if __name__ == "__main__":
    unittest.main()

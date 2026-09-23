#!/usr/bin/env python3
"""Control-plane boot-order contracts: adb and networking come up first.

The management plane must not depend on the storage waits that precede it or on
the service graph that follows it. A candidate boot that never reached
startup-ready was unreachable for minutes because nothing recorded whether adb
and the network had come up, and because adbd sat behind the expdb and userdata
waits.
"""
from pathlib import Path
import re
import unittest

HERE = Path(__file__).resolve().parent
INIT = HERE / "initramfs/libreecho-init"

PMSG_TOKEN = re.compile(r"^[A-Za-z0-9._:/=-]{1,160}$")


class EarlyControlPlaneContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.init = INIT.read_text()

    # ---------------------------------------------------------------- adbd
    def test_adb_gadget_precedes_the_storage_waits(self) -> None:
        adb = self.init.index("# Linux 6.1 has no Android android_usb class.")
        expdb = self.init.index("EXPDB_SYS=/sys/class/block/mmcblk0p7")
        userdata = self.init.index("if userdata_mount; then")
        self.assertLess(adb, expdb)
        self.assertLess(adb, userdata)

    def test_adbd_setup_precedes_the_service_graph(self) -> None:
        adb = self.init.index("# Linux 6.1 has no Android android_usb class.")
        graph = self.init.index("apply_timezone()")
        self.assertLess(adb, graph)
        # adbd needs Android's property area, which must already exist.
        props = self.init.index("# the property area needed by adbd")
        self.assertLess(props, adb)

    def test_adb_readiness_is_persisted_as_tokens(self) -> None:
        for marker in (
            "adb-ffs-ready",
            "adb-ffs-not-ready",
            "adb-tcp-5555-bound",
            "adb-tcp-5555-unbound",
        ):
            self.assertIn(f"pmsg_marker {marker}", self.init)
            self.assertRegex(marker, PMSG_TOKEN)
        # The bind probe reads the kernel's own listener table.
        self.assertIn("grep -q ':15B3 ' /proc/net/tcp", self.init)

    # ------------------------------------------------------------ networking
    def test_network_activation_runs_on_the_boot_path(self) -> None:
        activation = self.init.index("start_wifi_network >>/tmp/wifi-boot.log 2>&1 &")
        self.assertIn("log wifi-boot-activation-started", self.init)
        self.assertIn("pmsg_marker wifi-boot-activation", self.init)
        # Only ever started once, and before the service graph.
        self.assertEqual(self.init.count("start_wifi_network >>"), 1)
        self.assertLess(activation, self.init.index("apply_timezone()"))

    def test_network_activation_after_the_wmt_nodes_and_vendor_assets(self) -> None:
        activation = self.init.index("wifi-boot-activation-started")
        self.assertLess(self.init.index("log wmt-nodes-created"), activation)
        # The activation refuses to run without the owner-local firmware.
        self.assertIn('if [ "${VENDOR_ASSETS_OK:-0}" -eq 1 ]; then', self.init)

    def test_loopback_is_up_before_the_network_starts(self) -> None:
        loopback = self.init.index("ifconfig lo 127.0.0.1 up")
        activation = self.init.index("start_wifi_network >>/tmp/wifi-boot.log 2>&1 &")
        self.assertLess(loopback, activation)

    def test_single_shot_wmt_claim_is_preserved(self) -> None:
        # A failed WMT activation must still refuse a second attempt in the same
        # boot; the early start goes through the same claim, not around it.
        self.assertIn("$BB mkdir /tmp/wifi.activation.claim", self.init)
        self.assertIn("start_wifi_network()", self.init)
        self.assertNotIn("start_wifi_network_sequence >>", self.init)

    def test_manual_request_path_still_works(self) -> None:
        # ADB keeps its operator-triggered activation when the claim is free.
        self.assertIn("wifi_request_loop &", self.init)
        self.assertIn("/tmp/wifi.request", self.init)


if __name__ == "__main__":
    unittest.main()

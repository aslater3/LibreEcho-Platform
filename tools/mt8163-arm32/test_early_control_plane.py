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
    def test_exactly_one_automatic_activation_exists(self) -> None:
        # Match the call wherever it appears in a command position. An earlier
        # version anchored at the start of the line and so missed
        # "$BB start_wifi_network &" or "busybox start_wifi_network &",
        # which is how a duplicate could have slipped through again. re.M is
        # required: without it "^" only matches the start of the whole file, so
        # an indented call would be missed as well.
        invocation = re.compile(
            r"(?:^[ \t]*|[;&|]\s*|\bexec\s+|\bbusybox\s+|\$BB\s+)"
            r"start_wifi_network(?=[ \t;&|]|$)",
            re.M,
        )
        calls = [
            match.start()
            for match in invocation.finditer(self.init)
            # the function definition and its internal sequence call are not
            # invocations of the automatic/operator entry point
            if "()" not in self.init[match.start() - 2 : match.start() + 20]
            and "start_wifi_network_sequence" not in self.init[match.start() : match.start() + 40]
        ]
        self.assertEqual(len(calls), 2, [self.init[c : c + 60] for c in calls])
        # Classify by the enclosing function body, not by file position:
        # wifi_request_loop is *defined* before the boot-path activation but
        # *called* after it, so an ordering test picked the wrong one.
        loop_start = self.init.index("wifi_request_loop()")
        loop_end = self.init.index("\n}\n", loop_start)
        manual = [c for c in calls if loop_start < c < loop_end]
        automatic = [c for c in calls if c not in manual]
        self.assertEqual(len(manual), 1)
        self.assertEqual(len(automatic), 1)
        self.assertIn(
            "start_wifi_network >>/tmp/wifi-boot.log 2>&1 &",
            self.init[automatic[0] : automatic[0] + 80],
        )
        self.assertIn("start_wifi_network_sequence", self.init)
        self.assertEqual(self.init.count("start_wifi_network >>/tmp/wifi-boot.log 2>&1 &"), 1)

    def test_wifi_activation_conditions_are_all_present(self) -> None:
        """A self-review found the FunctionFS condition dropped from the chain.

        ffs_ready happens to be 1 at this point, so the call still works, but
        the condition had been replaced by a comment claiming it was satisfied
        "by construction". Assert each condition exists.
        """
        activation = self.init.index("start_wifi_network >>/tmp/wifi-boot.log 2>&1 &")
        chain = self.init.rindex('if [ "${VENDOR_ASSETS_OK:-0}" -ne 1 ]; then', 0, activation)
        block = self.init[chain : activation + 200]
        self.assertIn('if [ "${VENDOR_ASSETS_OK:-0}" -ne 1 ]; then', block)
        self.assertIn('elif [ "$SERVICE_PROFILE" = diagnostic ]; then', block)
        self.assertIn('elif [ "${ffs_ready:-0}" -eq 1 ]; then', block)
        self.assertIn("log wifi-network-skipped-vendor-assets-unavailable", block)
        self.assertIn("log wifi-network-policy-manual-single-shot", block)
        self.assertIn("log wifi-network-worker-started-after-adb", block)
        self.assertIn("log wifi-network-skipped-adb-not-ready", block)
        # ffs_ready is initialised and set before this chain, so the guard is
        # meaningful rather than always true.
        self.assertLess(self.init.index("ffs_ready=0"), chain)
        self.assertLess(self.init.index("ffs_ready=1"), chain)

    def test_network_activation_runs_on_the_boot_path(self) -> None:
        activation = self.init.index("start_wifi_network >>/tmp/wifi-boot.log 2>&1 &")
        self.assertIn("pmsg_marker wifi-boot-activation", self.init)
        self.assertLess(activation, self.init.index("apply_timezone()"))
        self.assertLess(activation, self.init.index("wifi_request_loop &"))

    def test_network_activation_keeps_the_profile_and_firmware_policy(self) -> None:
        activation = self.init.index("start_wifi_network >>/tmp/wifi-boot.log 2>&1 &")
        guard = self.init.rindex('if [ "${VENDOR_ASSETS_OK:-0}" -ne 1 ]; then', 0, activation)
        self.assertLess(guard, activation)
        # The activation is the last branch of the policy chain, not an
        # unconditional call: diagnostic images stay manual/single-shot.
        branch = self.init[guard:activation]
        self.assertIn('elif [ "$SERVICE_PROFILE" = diagnostic ]; then', branch)
        self.assertIn("log wifi-network-policy-manual-single-shot", branch)
        self.assertIn('elif [ "${ffs_ready:-0}" -eq 1 ]; then', branch)
        # And it still happens after the WMT nodes exist.
        self.assertLess(self.init.index("log wmt-nodes-created"), activation)

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

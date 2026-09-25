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
import shlex
import shutil
import subprocess
import tempfile
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
            "adb-usb-endpoints-ready",
            "adb-usb-endpoints-missing",
            "adb-tcp-not-configured",
        ):
            self.assertIn(f"pmsg_marker {marker}", self.init)
            self.assertRegex(marker, PMSG_TOKEN)
        # The TCP markers are only emitted when the image configures a listener.
        for marker in ("adb-tcp-5555-bound", "adb-tcp-5555-unbound"):
            self.assertIn(f"pmsg_marker {marker}", self.init)
            self.assertRegex(marker, PMSG_TOKEN)

    def test_adb_transport_policy_is_read_not_inferred(self) -> None:
        """A USB-only build must not report an absent TCP listener as a failure."""
        self.assertIn("ADBD_TRANSPORT_FILE=/etc/libreecho/adb-transport", self.init)
        self.assertIn("adb-transport-policy-read", self.init)
        self.assertIn("adb-transport-policy-absent", self.init)
        # The TCP probe is gated on the configured policy.
        policy = self.init.index("adbd_tcp_configured=")
        gate = self.init.index('if [ "$adbd_tcp_configured" -eq 1 ]; then')
        self.assertLess(policy, gate)
        self.assertIn("pmsg_marker adb-tcp-not-configured", self.init)
        # No blanket 5-second wait on a USB-only boot.
        self.assertNotIn("grep -q ':15B3 ' /proc/net/tcp", self.init)

    def test_dev_tcp_policy_rejects_stable_missing_and_malformed_records(self) -> None:
        start = self.init.index("adbd_dev_tcp_policy()")
        body = self.init[start : self.init.index("\n}\n", start) + len("\n}\n")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            channel_file = root / "channel"
            policy_file = root / "adb-transport"
            body = body.replace("/etc/libreecho/update-channel", str(channel_file))
            for channel, policy, expected in (
                ("dev", "schema=1\ntransport=usb-functionfs-and-tcp\ntcp_listener=true\ntcp_port=5555\n", True),
                ("stable", "schema=1\ntransport=usb-functionfs-and-tcp\ntcp_listener=true\ntcp_port=5555\n", False),
                ("dev", "schema=1\ntransport=usb-functionfs-only\ntcp_listener=false\ntcp_port=0\n", False),
                ("dev", "schema=1\ntransport=usb-functionfs-and-tcp\ntcp_listener=true\ntcp_port=5556\n", False),
            ):
                channel_file.write_text(channel)
                policy_file.write_text(policy)
                proc = subprocess.run(
                    ["sh", "-c", body + "\nadbd_dev_tcp_policy"],
                    env={"PATH": "/usr/bin:/bin", "BB": "", "ADBD_TRANSPORT_FILE": str(policy_file)},
                    capture_output=True, text=True,
                )
                self.assertEqual(proc.returncode == 0, expected, (channel, policy, proc.stderr))
            channel_file.unlink()
            self.assertNotEqual(subprocess.run(
                ["sh", "-c", body + "\nadbd_dev_tcp_policy"],
                env={"PATH": "/usr/bin:/bin", "BB": "", "ADBD_TRANSPORT_FILE": str(policy_file)},
                capture_output=True,
            ).returncode, 0)

    def test_dev_tcp_fallback_does_not_start_stable_adbd(self) -> None:
        self.assertEqual(self.init.count("/sbin/adbd --device_banner=device"), 1)
        self.assertIn('if [ "$adbd_tcp_configured" -eq 1 ] &&', self.init)
        self.assertIn("log adbd-tcp-fallback-started", self.init)
        self.assertIn("adb_listening && $BB kill -0", self.init)
        self.assertIn("last_check=adb-tcp-ready", self.init)
        self.assertIn("last_check=ffs-ready", self.init)

    def test_adb_listener_check_requires_listen_state_and_local_port(self) -> None:
        """A connection to 5555, or a client socket, is not a listener."""
        start = self.init.index("adb_listening()")
        body = self.init[start : self.init.index("\n}\n", start)]
        self.assertIn('NR > 1 && $4 == "0A"', body)
        # A local listener may be wildcard or loopback; the remote side of a
        # listening row must be the wildcard, so a client socket is excluded.
        self.assertIn(
            'l[2] == "15B3" && (l[1] == "00000000" || l[1] == "0100007F")', body
        )
        self.assertIn('r[2] == "15B3" && r[1] == "00000000"', body)

    def test_adb_listener_check_behaviour_on_real_proc_net_tcp(self) -> None:
        """Run the shipped predicate against representative /proc/net/tcp rows."""
        busybox = shutil.which("busybox") or ""
        start = self.init.index("adb_listening()")
        # Include the closing brace: the function is executed, not inspected.
        body = self.init[start : self.init.index("\n}\n", start) + len("\n}\n")]
        cases = {
            "listen_wildcard": (
                "  sl local rem st\n"
                "   0: 00000000:15B3 00000000:0000 0A 0 0 0 0 0 0 1\n", True
            ),
            "listen_loopback": (
                "  sl local rem st\n"
                "   1: 0100007F:15B3 00000000:0000 0A 0 0 0 0 0 0 1\n", True
            ),
            "established_to_5555": (
                "  sl local rem st\n"
                "   2: 0100007F:1F91 0100007F:15B3 01 0 0 0 0 0 0 1\n", False
            ),
            "client_socket": (
                "  sl local rem st\n"
                "   3: 0A000005:1F90 0200A8C0:C1FE 01 0 0 0 0 0 0 1\n", False
            ),
            "other_port_listening": (
                "  sl local rem st\n"
                "   4: 00000000:1F90 00000000:0000 0A 0 0 0 0 0 0 1\n", False
            ),
        }
        for label, (table, expected) in cases.items():
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "tcp"
                path.write_text(table)
                script = (
                    f"BB={shlex.quote(busybox)}\n"
                    + body.replace("/proc/net/tcp /proc/net/tcp6", shlex.quote(str(path)))
                    + "\nadb_listening\n"
                    'echo "adb_listening_rc=$?"\n'
                )
                result = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
                # adb_listening exits 0 when a listener was found.
                found = "adb_listening_rc=0" in result.stdout
                self.assertEqual(
                    found, bool(expected), f"{label}: {result.stdout}{result.stderr}"
                )

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
        self.assertIn('elif [ "$adb_control_ready" -eq 1 ]; then', block)
        self.assertIn('[ "${ffs_ready:-0}" -eq 1 ] && adb_control_ready=1', self.init)
        self.assertIn('adb_tcp_ready && adb_control_ready=1', self.init)
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
        self.assertIn('elif [ "$adb_control_ready" -eq 1 ]; then', branch)
        self.assertIn('[ "${ffs_ready:-0}" -eq 1 ] && adb_control_ready=1', self.init)
        self.assertIn('adb_tcp_ready && adb_control_ready=1', self.init)
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

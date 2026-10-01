#!/usr/bin/env python3
"""Control-plane boot-order contracts: adb and networking come up first.

The management plane must not depend on the storage waits that precede it or on
the service graph that follows it. A candidate boot that never reached
startup-ready was unreachable for minutes because nothing recorded whether adb
and the network had come up, and because adbd sat behind the expdb and userdata
waits.
"""
from pathlib import Path
import os
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
        # The eMMC block-node wait (formerly the expdb wait, removed for
        # Platform #195) is the first storage wait and must follow adbd.
        storage = self.init.index("[ ! -r /sys/class/block/mmcblk0p16/dev ]")
        userdata = self.init.index("if userdata_mount; then")
        self.assertLess(adb, storage)
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

    # ------------------------------------------------- recovery-boot coordination
    def test_recovery_owns_radio_reads_the_boot_marker(self) -> None:
        start = self.init.index("recovery_owns_radio()")
        body = self.init[start : self.init.index("\n}\n", start) + len("\n}\n")]
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "recovery-mode"
            script = (
                f"PHYSICAL_RECOVERY_MARKER={shlex.quote(str(marker))}\n"
                + body
                + "\nif recovery_owns_radio; then echo owned; else echo free; fi\n"
            )
            free = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
            self.assertEqual(free.stdout.strip(), "free", free.stderr)
            marker.write_text("libreecho-recovery-v1\n")
            owned = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
            self.assertEqual(owned.stdout.strip(), "owned", owned.stderr)

    def test_client_start_is_suppressed_on_a_recovery_boot(self) -> None:
        guard = self.init.index("if recovery_owns_radio; then")
        client = self.init.index('WIFI_CONF="$wifi_profile" /sbin/libreecho-wifi start')
        self.assertLess(guard, client)
        between = self.init[guard:client]
        self.assertIn("wifi-client-suppressed-recovery-boot", between)
        self.assertIn("return 0", between)
        # Exactly one guard, and it sits after the driver bring-up that creates
        # wlan0, so the AP probe and hostapd still find the interface.
        self.assertEqual(self.init.count("if recovery_owns_radio; then"), 1)
        self.assertLess(self.init.index("wlan0-registration-timeout"), guard)
        # Normal boot is unchanged: the client start is still present exactly
        # once, not replaced by the suppression.
        self.assertEqual(
            self.init.count('WIFI_CONF="$wifi_profile" /sbin/libreecho-wifi start'), 1)

    def test_wifi_profile_is_recorded_before_the_client_start(self) -> None:
        record = self.init.index("WIFI_PROFILE_STATE=/run/libreecho/wifi-client.conf")
        guard = self.init.index("if recovery_owns_radio; then")
        client = self.init.index('WIFI_CONF="$wifi_profile" /sbin/libreecho-wifi start')
        self.assertLess(record, guard)
        self.assertLess(record, client)
        self.assertIn(
            "printf '%s\\n' \"$wifi_profile\" > \"$WIFI_PROFILE_STATE\"", self.init)

    # ---------------------------------------- recovery-boot radio ordering
    def test_recovery_boot_waits_for_the_radio_before_networkd(self) -> None:
        """A recovery boot's service graph must not probe the radio too early.

        The boot-path Wi-Fi worker records a boot-scoped radio-ready marker once
        wlan0 exists, and the graph waits (bounded) for it before starting
        networkd -- whose AP capability probe would otherwise report the
        interface absent during the WMT load / wlan0 wait and leave the portal
        unavailable for the whole boot.  An ordinary boot has no recovery
        marker, so the wait is a no-op and the ordering is unchanged.
        """
        # The marker can only be recorded after the bounded wlan0 wait, so it
        # implies the interface exists.
        wlan0_wait = self.init.index("while [ \"$i\" -lt 30 ] && [ ! -e /sys/class/net/wlan0 ]")
        marker_default = self.init.index("RECOVERY_WLAN0_MARKER=")
        recorded = self.init.index("log wlan0-ready-recorded")
        self.assertLess(wlan0_wait, recorded)
        self.assertLess(marker_default, recorded)
        # networkd's start is gated on the wait...
        ui = self.init.index("start_ui_services()")
        ui_body = self.init[ui:self.init.index("\n}\n", ui)]
        gate = ui_body.index('if [ "$service" = networkd ]; then')
        self.assertIn("recovery_radio_ready_wait", ui_body[gate:])
        # ...and the wait itself is recovery-gated: an ordinary boot returns
        # without touching the marker or sleeping.
        helper = self.init.index("recovery_radio_ready_wait()")
        helper_body = self.init[helper:self.init.index("\n}\n", helper)]
        self.assertIn("recovery_owns_radio || return 0", helper_body)
        self.assertIn("RECOVERY_RADIO_WAIT", helper_body)
        # The wait synchronizes on the worker's terminal outcome: readiness
        # succeeds, a recorded terminal failure fails, and neither path may
        # claim success after the bound expires.
        self.assertIn("RECOVERY_WIFI_FAILED_MARKER", helper_body)
        self.assertIn("log recovery-radio-ready-failed", helper_body)
        # The graph reports an unavailable radio truthfully instead of starting
        # networkd as if readiness had been proven.
        self.assertIn("if ! recovery_radio_ready_wait; then", ui_body)
        self.assertIn("log ui-networkd-radio-unavailable", ui_body)

    def test_recovery_radio_ready_wait_is_bounded_and_recovery_gated(self) -> None:
        start = self.init.index("recovery_owns_radio()")
        owns = self.init[start:self.init.index("\n}\n", start) + len("\n}\n")]
        helper_start = self.init.index("recovery_radio_ready_wait()")
        helper = self.init[
            helper_start:self.init.index("\n}\n", helper_start) + len("\n}\n")]

        # The default bound must cover the worker's complete worst-case
        # sequence: the 1s stale-launcher settle, the 1s responder settle, up
        # to 30s for the function-on write, up to 10s waiting for the
        # responder to exit, and up to 30s waiting for wlan0 (72s total).
        default = re.search(
            r"RECOVERY_RADIO_WAIT=\$\{RECOVERY_RADIO_WAIT:-([0-9]+)\}",
            self.init)
        self.assertIsNotNone(default)
        bound_default = int(default.group(1)) if default else 0
        self.assertGreaterEqual(bound_default, 72)
        # The gate may only report success when the worker recorded readiness.
        # A terminal worker failure and the bound expiring are truthful
        # failures, not blind successes.
        self.assertIn('if [ -f "$RECOVERY_WIFI_FAILED_MARKER" ]; then', helper)
        failed = helper.index("log recovery-radio-ready-failed")
        self.assertIn("return 1", helper[failed:])
        timeout = helper.index("log recovery-radio-ready-timeout")
        self.assertIn("return 1", helper[timeout:])
        self.assertNotIn("return 0", helper[timeout:])

        def run_case(recovery_marker: bool, *, bound: int,
                     ready_marker: bool = False, failed_marker: bool = False,
                     ready_at: str = "", fail_at: str = ""):
            """Advance the clock without real sleeping.

            The worker's delayed readiness or terminal failure is published by
            the accelerated clock at a sleep ordinal, so the whole sequence is
            exercised with no hardware and no waiting.
            """
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                recovery = root / "recovery-mode"
                ready = root / "wlan0-ready"
                failed = root / "wifi-worker-failed"
                sleep_log = root / "sleeps"
                busybox = root / "busybox"
                busybox.write_text(
                    "#!/bin/sh\n"
                    "if [ \"$1\" = sleep ]; then\n"
                    "  n=$(cat \"$SLEEP_LOG\" 2>/dev/null || printf 0)\n"
                    "  n=$((n + 1))\n"
                    "  printf '%s' \"$n\" > \"$SLEEP_LOG\"\n"
                    "  if [ -n \"${READY_AT:-}\" ] && [ \"$n\" -ge \"$READY_AT\" ]; then\n"
                    "    : > \"$READY_MARKER_PATH\"\n"
                    "  fi\n"
                    "  if [ -n \"${FAIL_AT:-}\" ] && [ \"$n\" -ge \"$FAIL_AT\" ]; then\n"
                    "    : > \"$FAILED_MARKER_PATH\"\n"
                    "  fi\n"
                    "fi\n"
                    "exit 0\n")
                busybox.chmod(0o755)
                if recovery_marker:
                    recovery.write_text("libreecho-recovery-v1\n")
                if ready_marker:
                    ready.touch()
                if failed_marker:
                    failed.write_text("2\n")
                script = (
                    "PHYSICAL_RECOVERY_MARKER=%s\n"
                    "RECOVERY_WLAN0_MARKER=%s\n"
                    "RECOVERY_WIFI_FAILED_MARKER=%s\n"
                    "RECOVERY_RADIO_WAIT=%d\n"
                    "BB=%s\n"
                    "log() { printf 'log:%%s\\n' \"$1\"; }\n"
                    % (shlex.quote(str(recovery)), shlex.quote(str(ready)),
                       shlex.quote(str(failed)), bound,
                       shlex.quote(str(busybox)))
                    + owns + helper
                    + "\nrecovery_radio_ready_wait; echo rc=$?\n")
                env = dict(os.environ)
                env["SLEEP_LOG"] = str(sleep_log)
                env["READY_MARKER_PATH"] = str(ready)
                env["FAILED_MARKER_PATH"] = str(failed)
                env["READY_AT"] = ready_at
                env["FAIL_AT"] = fail_at
                result = subprocess.run(["sh", "-c", script], env=env,
                                        capture_output=True, text=True)
                sleeps = int(sleep_log.read_text()) if sleep_log.exists() else 0
                return result, sleeps

        def rc_of(result):
            return result.stdout.strip().splitlines()[-1]

        # Ordinary boot: no recovery marker, so the wait is a no-op.
        result, sleeps = run_case(False, bound=90)
        self.assertEqual(rc_of(result), "rc=0", result.stderr)
        self.assertEqual(sleeps, 0)
        # Recovery boot with the radio already up: returns immediately.
        result, sleeps = run_case(True, bound=90, ready_marker=True)
        self.assertEqual(rc_of(result), "rc=0", result.stderr)
        self.assertEqual(sleeps, 0)
        # Recovery boot whose worker terminally failed before the graph ran:
        # no sitting out the bound, no blind success.
        result, sleeps = run_case(True, bound=90, failed_marker=True)
        self.assertEqual(rc_of(result), "rc=1", result.stderr)
        self.assertIn("log:recovery-radio-ready-failed", result.stdout)
        self.assertEqual(sleeps, 0)
        # Delayed worker readiness beyond the old 30s bound: the accelerated
        # clock publishes readiness at tick 45, and the wait must cover the
        # complete sequence and only succeed once readiness is recorded.
        result, sleeps = run_case(True, bound=90, ready_at="45")
        self.assertEqual(rc_of(result), "rc=0", result.stderr)
        self.assertNotIn("recovery-radio-ready-timeout", result.stdout)
        self.assertNotIn("recovery-radio-ready-failed", result.stdout)
        self.assertEqual(sleeps, 45)
        # A worker that terminally fails at tick 12: the wait ends with the
        # worker's outcome, reports the failure truthfully, and neither sits
        # out the bound nor claims success.
        result, sleeps = run_case(True, bound=90, fail_at="12")
        self.assertEqual(rc_of(result), "rc=1", result.stderr)
        self.assertIn("log:recovery-radio-ready-failed", result.stdout)
        self.assertNotIn("recovery-radio-ready-timeout", result.stdout)
        self.assertEqual(sleeps, 12)
        # No terminal outcome within a short bound: bounded, truthful timeout.
        result, sleeps = run_case(True, bound=5)
        self.assertEqual(rc_of(result), "rc=1", result.stderr)
        self.assertIn("log:recovery-radio-ready-timeout", result.stdout)
        self.assertEqual(sleeps, 5)


class RecoveryWorkerRadioPathTests(unittest.TestCase):
    """Execute the boot-path Wi-Fi worker instead of matching source strings.

    A fresh device has no /data and no packaged client profile.  The worker
    must still initialize the radio on a recovery boot -- that is what creates
    wlan0 for the AP capability probe and hostapd -- while never starting the
    client supplicant.  The shipped functions run in a host sandbox with an
    accelerated clock, so the no-profile recovery path, the profile-bearing
    recovery path, and the ordinary-boot profile selection are all exercised
    behaviourally; nothing here runs on hardware.
    """

    REPLACEMENTS = (
        ("/dev/wmtWifi", "$SB/dev/wmtWifi"),
        ("/sbin/wmt_stock_compat", "$SB/sbin/wmt_stock_compat"),
        ("/sbin/wmt_launcher", "$SB/sbin/wmt_launcher"),
        ("/sbin/libreecho-wifi", "$SB/sbin/libreecho-wifi"),
        ("/sys/class/net/wlan0", "$SB/sys/class/net/wlan0"),
        ("/run/libreecho", "$SB/run"),
        ("/tmp/wifi", "$SB/tmp/wifi"),
        ("/proc/mounts", "$SB/proc-mounts"),
        ("/etc/wifi/wpa_supplicant.conf", "$SB/etc/wifi/wpa_supplicant.conf"),
        ("/data/libreecho/config/wpa_supplicant.conf",
         "$SB/data/wpa_supplicant.conf"),
        ("/bin/busybox sh -c", "sh -c"),
    )

    def setUp(self) -> None:
        self.init = INIT.read_text()

    def _function(self, name: str) -> str:
        start = self.init.index(name)
        return self.init[start:self.init.index("\n}\n", start) + len("\n}\n")]

    @staticmethod
    def _stub(path: Path, text: str) -> None:
        path.write_text(text)
        path.chmod(0o755)

    @staticmethod
    def _bind(body: str) -> str:
        for old, new in RecoveryWorkerRadioPathTests.REPLACEMENTS:
            body = body.replace(old, new)
        return body

    def run_worker(self, *, recovery: bool, packaged_profile: bool,
                   wlan0: bool = True, entry: str = "sequence",
                   wmt_fails: bool = False,
                   vendor_assets_ok: bool = True) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for directory in ("bin", "sbin", "dev", "etc/wifi", "run", "tmp",
                              "sys/class/net"):
                (root / directory).mkdir(parents=True, exist_ok=True)
            (root / "proc-mounts").write_text(
                "rootfs / rootfs rw 0 0\nproc /proc proc rw 0 0\n")
            if packaged_profile:
                (root / "etc/wifi/wpa_supplicant.conf").write_text(
                    "ssid=test\n")
            if wlan0:
                (root / "sys/class/net/wlan0").touch()
            if recovery:
                (root / "run/recovery-mode").write_text(
                    "libreecho-recovery-v1\n")

            # The fake BusyBox compresses the clock and stands in for the
            # process primitives the worker uses: sleep is a short real delay,
            # pidof/kill track the responder stub's pidfile, and timeout runs
            # the bounded command directly.
            self._stub(
                root / "bin/busybox",
                "#!/bin/sh\n"
                "applet=$1\n"
                "shift\n"
                "case \"$applet\" in\n"
                "    sleep)\n"
                "        i=0\n"
                "        while [ -n \"${FAKE_WAIT_PIDFILE:-}\" ] &&\n"
                "              [ ! -f \"$FAKE_PIDFILE\" ] && [ \"$i\" -lt 200 ]; do\n"
                "            sleep 0.01\n"
                "            i=$((i + 1))\n"
                "        done\n"
                "        sleep 0.05\n"
                "        exit 0\n"
                "        ;;\n"
                "    pidof)\n"
                "        [ -f \"$FAKE_PIDFILE\" ] || exit 1\n"
                "        cat \"$FAKE_PIDFILE\"\n"
                "        exit 0\n"
                "        ;;\n"
                "    kill)\n"
                "        if [ \"$1\" = \"-0\" ]; then\n"
                "            [ -f \"$FAKE_PIDFILE\" ] || exit 1\n"
                "        fi\n"
                "        exit 0\n"
                "        ;;\n"
                "    timeout)\n"
                "        shift\n"
                "        exec \"$@\"\n"
                "        ;;\n"
                "    *)\n"
                "        exec \"$(command -v \"$applet\")\" \"$@\"\n"
                "        ;;\n"
                "esac\n")
            self._stub(
                root / "sbin/wmt_stock_compat",
                "#!/bin/sh\n"
                "printf 'configure\\n' > \"$WMT_CONFIGURE_LOG\"\n"
                "exit " + ("1" if wmt_fails else "0") + "\n")
            self._stub(
                root / "sbin/wmt_launcher",
                "#!/bin/sh\n"
                "printf '%s' \"$$\" > \"$FAKE_PIDFILE\"\n"
                "i=0\n"
                "while [ ! -e \"$FAKE_WMTWIFI\" ] && [ \"$i\" -lt 400 ]; do\n"
                "    sleep 0.01\n"
                "    i=$((i + 1))\n"
                "done\n"
                "rm -f \"$FAKE_PIDFILE\"\n"
                "exit 0\n")
            self._stub(
                root / "sbin/libreecho-wifi",
                "#!/bin/sh\n"
                "printf 'conf=%s args=%s\\n' \"${WIFI_CONF:-}\" \"$*\""
                " > \"$WIFI_CLIENT_LOG\"\n"
                "exit 0\n")

            sequence = self._function("start_wifi_network_sequence()")
            # Every binding must be present in the shipped sequence, or the
            # path under test silently changes.
            for old, new in self.REPLACEMENTS:
                self.assertIn(old, sequence, old)
                sequence = sequence.replace(old, new)
            functions = self._function("recovery_owns_radio()")
            functions += sequence
            call = "start_wifi_network_sequence"
            if entry == "activation":
                functions += self._bind(self._function(
                    "wifi_worker_record_failure()"))
                functions += self._bind(self._function("start_wifi_network()"))
                call = "start_wifi_network"

            script = (
                "SB=" + shlex.quote(str(root)) + "\n"
                "BB=$SB/bin/busybox\n"
                "export FAKE_PIDFILE=\"$SB/run/responder.pid\"\n"
                "export FAKE_WMTWIFI=\"$SB/dev/wmtWifi\"\n"
                "export FAKE_WAIT_PIDFILE=1\n"
                "export WMT_CONFIGURE_LOG=\"$SB/wmt-configure.log\"\n"
                "export WIFI_CLIENT_LOG=\"$SB/client.log\"\n"
                "PHYSICAL_RECOVERY_MARKER=\"$SB/run/recovery-mode\"\n"
                "RECOVERY_WLAN0_MARKER=\"$SB/run/wlan0-ready\"\n"
                "RECOVERY_WIFI_FAILED_MARKER=\"$SB/run/wifi-worker-failed\"\n"
                "VENDOR_ASSETS_OK=" + ("1" if vendor_assets_ok else "0") + "\n"
                "log() { printf 'log:%s\\n' \"$1\"; }\n"
                + functions
                + "\n" + call + "\nprintf 'worker_rc=%s\\n' \"$?\"\n")
            result = subprocess.run(["sh", "-c", script], capture_output=True,
                                    text=True, env=dict(os.environ))
            failed_marker = root / "run/wifi-worker-failed"
            profile_state = root / "run/wifi-client.conf"
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "rc": (result.stdout.strip().splitlines()[-1]
                       if result.stdout.strip() else ""),
                "wmt_configured": (root / "wmt-configure.log").exists(),
                "client_started": (root / "client.log").exists(),
                "client_conf": ((root / "client.log").read_text().strip()
                                if (root / "client.log").exists() else ""),
                "wlan0_marker": (root / "run/wlan0-ready").exists(),
                "failed_marker": (failed_marker.read_text().strip()
                                  if failed_marker.exists() else ""),
                "profile_state": (profile_state.read_text().strip()
                                  if profile_state.exists() else ""),
            }

    def test_no_profile_recovery_boot_initializes_the_radio(self) -> None:
        run = self.run_worker(recovery=True, packaged_profile=False)
        self.assertEqual(run["rc"], "worker_rc=0",
                         run["stdout"] + run["stderr"])
        # The profile gate must not stop the WMT initialization that creates
        # wlan0: a fresh device with no client profile is the normal recovery
        # case, and the AP capability probe needs the interface to exist.
        self.assertIn("log:wifi-profile-absent-recovery-boot\n", run["stdout"])
        self.assertNotIn("log:wifi-profile-absent\n", run["stdout"])
        self.assertIn("log:wlan0-ready-recorded", run["stdout"])
        self.assertTrue(run["wmt_configured"])
        self.assertTrue(run["wlan0_marker"])
        # The recovery portal owns the radio: no client supplicant is started,
        # and no handover profile record is written for a profile that does
        # not exist.
        self.assertIn("log:wifi-client-suppressed-recovery-boot", run["stdout"])
        self.assertFalse(run["client_started"])
        self.assertEqual(run["profile_state"], "")
        self.assertNotIn("wifi-profile-record-failed", run["stdout"])

    def test_profile_bearing_recovery_boot_is_unchanged(self) -> None:
        run = self.run_worker(recovery=True, packaged_profile=True)
        self.assertEqual(run["rc"], "worker_rc=0",
                         run["stdout"] + run["stderr"])
        self.assertIn("log:wifi-packaged-profile-selected", run["stdout"])
        self.assertIn("log:wifi-client-suppressed-recovery-boot", run["stdout"])
        self.assertFalse(run["client_started"])
        # The recorded handover profile is still written for the teardown.
        self.assertTrue(
            run["profile_state"].endswith("etc/wifi/wpa_supplicant.conf"))

    def test_ordinary_boot_profile_selection_is_unchanged(self) -> None:
        run = self.run_worker(recovery=False, packaged_profile=True)
        self.assertEqual(run["rc"], "worker_rc=0",
                         run["stdout"] + run["stderr"])
        self.assertIn("log:wifi-packaged-profile-selected", run["stdout"])
        self.assertNotIn("wifi-client-suppressed", run["stdout"])
        self.assertTrue(run["client_started"])
        self.assertIn("etc/wifi/wpa_supplicant.conf args=start",
                      run["client_conf"])
        self.assertTrue(run["profile_state"])

    def test_ordinary_boot_without_a_profile_still_refuses(self) -> None:
        run = self.run_worker(recovery=False, packaged_profile=False)
        self.assertEqual(run["rc"], "worker_rc=2",
                         run["stdout"] + run["stderr"])
        self.assertIn("log:wifi-profile-absent\n", run["stdout"])
        # The radio is not initialized and no client is started.
        self.assertFalse(run["wmt_configured"])
        self.assertFalse(run["wlan0_marker"])
        self.assertFalse(run["client_started"])

    def test_terminal_worker_failures_are_recorded_for_the_gate(self) -> None:
        # A failing WMT configure step is a terminal activation failure; the
        # activation records the outcome the recovery gate waits on.
        run = self.run_worker(recovery=True, packaged_profile=False,
                              entry="activation", wmt_fails=True)
        self.assertEqual(run["rc"], "worker_rc=1",
                         run["stdout"] + run["stderr"])
        self.assertIn("log:wifi-activation-complete:1", run["stdout"])
        self.assertIn("log:wifi-worker-failed-recorded:1", run["stdout"])
        self.assertEqual(run["failed_marker"], "1")
        # The vendor-assets refusal inside the activation is recorded the same
        # way, without running the radio sequence at all.
        run = self.run_worker(recovery=True, packaged_profile=False,
                              entry="activation", vendor_assets_ok=False)
        self.assertEqual(run["rc"], "worker_rc=4",
                         run["stdout"] + run["stderr"])
        self.assertIn("log:wifi-worker-failed-recorded:4", run["stdout"])
        self.assertEqual(run["failed_marker"], "4")
        self.assertFalse(run["wmt_configured"])


if __name__ == "__main__":
    unittest.main()

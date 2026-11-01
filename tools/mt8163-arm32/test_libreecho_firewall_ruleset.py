#!/usr/bin/env python3
"""Host tests for the LibreEcho default inbound firewall.

The Python renderer is the reference.  The boot script initramfs/libreecho-firewall
must emit byte-identical rulesets for every feature combination, and must do
nothing harmful when nft is missing or the owner disable switch is present.
Nothing here loads a ruleset into a kernel; that needs a device.
"""

import itertools
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import libreecho_firewall_ruleset as r
from libreecho_firewall_ruleset import TABLE, Features, allowed_ports, render_ruleset


HERE = Path(__file__).resolve().parent
BOOT_SCRIPT = HERE / "initramfs" / "libreecho-firewall"
FEATURE_FLAGS = {
    "esphome": "--esphome",
    "airplay": "--airplay",
    "ptp": "--ptp",
    "ssh": "--ssh",
    "adb_tcp": "--adb-tcp",
    "recovery_ap": "--recovery-ap",
}


def accepts(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip().endswith("accept")]


class RulesetRenderingTests(unittest.TestCase):
    def test_default_policy_is_drop_on_inet_input(self) -> None:
        text = render_ruleset(Features())
        self.assertIn(f"table inet {TABLE} {{", text)
        self.assertIn("type filter hook input priority 0; policy drop;", text)

    def test_table_is_replaced_not_flushed_globally(self) -> None:
        text = render_ruleset(Features())
        self.assertIn(f"add table inet {TABLE}\ndelete table inet {TABLE}\n", text)
        self.assertNotIn("flush ruleset", text)

    def test_loopback_and_established_related_are_always_allowed(self) -> None:
        text = render_ruleset(Features())
        self.assertIn('iifname "lo" accept', text)
        self.assertIn("ct state established,related accept", text)
        self.assertIn("ct state invalid drop", text)

    def test_icmpv6_is_allowed_for_ipv6_neighbour_discovery(self) -> None:
        self.assertIn("meta l4proto ipv6-icmp accept", render_ruleset(Features()))

    def test_default_ports_are_web_and_mdns_only(self) -> None:
        self.assertEqual(allowed_ports(Features()), ((8080,), (5353,)))

    def test_ssh_is_closed_unless_enabled(self) -> None:
        self.assertNotIn("dport 22", render_ruleset(Features()))
        self.assertIn("tcp dport { 22, 8080 } accept", render_ruleset(Features(ssh=True)))

    def test_adb_tcp_is_closed_unless_enabled(self) -> None:
        self.assertNotIn("5555", render_ruleset(Features()))
        self.assertIn("5555", render_ruleset(Features(adb_tcp=True)))

    def test_feature_flags_add_their_ports(self) -> None:
        tcp, udp = allowed_ports(Features(esphome=True, airplay=True, ptp=True))
        self.assertEqual(tcp, (6053, 7000, 8080))
        self.assertEqual(udp, (319, 320, 5353))

    def test_recovery_ap_allows_dhcp_and_dns(self) -> None:
        tcp, udp = allowed_ports(Features(recovery_ap=True))
        self.assertIn(53, tcp)
        self.assertIn(53, udp)
        self.assertIn(67, udp)

    def test_braces_balance_and_accept_rules_use_known_matches(self) -> None:
        text = render_ruleset(
            Features(esphome=True, airplay=True, ptp=True, ssh=True, adb_tcp=True)
        )
        self.assertEqual(text.count("{"), text.count("}"))
        for line in accepts(text):
            self.assertTrue(
                line.startswith(("iifname", "ct ", "meta ", "tcp dport", "udp dport")),
                line,
            )

    def test_port_range_is_validated(self) -> None:
        original = r.ALWAYS_TCP
        r.ALWAYS_TCP = (70000,)
        try:
            with self.assertRaises(ValueError):
                render_ruleset(Features())
        finally:
            r.ALWAYS_TCP = original


@unittest.skipUnless(shutil.which("sh"), "POSIX sh is required")
class BootScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fw-test-"))
        shim = self.tmp / "bb"
        # busybox applets are plain commands on the host; the script calls $BB <applet>.
        shim.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        self.env = dict(
            os.environ,
            LIBREECHO_FW_BB=str(shim),
            LIBREECHO_FW_NFT=str(self.tmp / "nft-missing"),
            LIBREECHO_FW_DISABLE=str(self.tmp / "disable-switch"),
            LIBREECHO_FW_RUN=str(self.tmp / "run"),
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(BOOT_SCRIPT), *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_shell_render_matches_python_for_every_feature_combination(self) -> None:
        names = list(FEATURE_FLAGS)
        for values in itertools.product((False, True), repeat=len(names)):
            kwargs = dict(zip(names, values))
            args = ["--render"] + [FEATURE_FLAGS[n] for n in names if kwargs[n]]
            with self.subTest(features=kwargs):
                shell = self.run_script(*args)
                self.assertEqual(shell.returncode, 0, shell.stderr)
                self.assertEqual(shell.stdout, render_ruleset(Features(**kwargs)))

    def test_missing_nft_is_logged_and_boot_continues(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nft unavailable", result.stdout)

    def test_owner_disable_switch_wins_and_exits_clean(self) -> None:
        (self.tmp / "disable-switch").write_text("", encoding="utf-8")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("disabled by owner switch", result.stdout)
        self.assertFalse((self.tmp / "run" / "firewall.nft").exists())

    def test_unknown_option_is_rejected(self) -> None:
        result = self.run_script("--bogus")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()

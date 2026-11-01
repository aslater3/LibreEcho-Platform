#!/usr/bin/env python3
"""Render the LibreEcho default inbound firewall as an nftables ruleset.

The ruleset is a default-deny `inet` input chain.  It allows loopback,
established/related traffic, ICMP needed for IPv6 neighbour discovery and ping,
and only the ports for services that are enabled.  The rendering is pure: no
device state is read here.  The boot script decides which features are enabled
and passes them in.

The output is applied atomically by `nft -f` in the boot script.  The table is
deleted and recreated, so re-running the script never leaves stale rules and
never flushes tables owned by other software.

This module does not prove the firewall works on a device.  That needs an
on-device ruleset load and a port scan from the release-candidate OTA pass.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path


TABLE = "libreecho_fw"

# Always-on services: the management UI and the shared mDNS responder.
ALWAYS_TCP = (8080,)
ALWAYS_UDP = (5353,)

# Feature-gated services.  Keys are feature names used by the boot script.
ESPHOME_TCP = (6053,)
AIRPLAY_TCP = (7000,)
PTP_UDP = (319, 320)
SSH_TCP = (22,)
ADB_TCP = (5555,)
RECOVERY_AP_UDP = (53, 67)
RECOVERY_AP_TCP = (53,)


@dataclasses.dataclass(frozen=True)
class Features:
    """Which optional inbound services are enabled for this boot."""

    esphome: bool = False
    airplay: bool = False
    ptp: bool = False
    ssh: bool = False
    adb_tcp: bool = False
    recovery_ap: bool = False


def _port_list(ports: tuple[int, ...]) -> str:
    for port in ports:
        if not 1 <= port <= 65535:
            raise ValueError(f"port out of range: {port}")
    if len(ports) == 1:
        return str(ports[0])
    return "{ " + ", ".join(str(p) for p in ports) + " }"


def allowed_ports(features: Features) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return (tcp_ports, udp_ports) allowed inbound for these features."""
    tcp = list(ALWAYS_TCP)
    udp = list(ALWAYS_UDP)
    if features.esphome:
        tcp += ESPHOME_TCP
    if features.airplay:
        tcp += AIRPLAY_TCP
    if features.ptp:
        udp += PTP_UDP
    if features.ssh:
        tcp += SSH_TCP
    if features.adb_tcp:
        tcp += ADB_TCP
    if features.recovery_ap:
        tcp += RECOVERY_AP_TCP
        udp += RECOVERY_AP_UDP
    return tuple(sorted(set(tcp))), tuple(sorted(set(udp)))


def render_ruleset(features: Features) -> str:
    """Return the nftables script for the given features."""
    tcp, udp = allowed_ports(features)
    lines = [
        "# LibreEcho default inbound firewall. Generated at boot; do not edit.",
        f"add table inet {TABLE}",
        f"delete table inet {TABLE}",
        f"table inet {TABLE} {{",
        "  chain input {",
        "    type filter hook input priority 0; policy drop;",
        '    iifname "lo" accept',
        "    ct state established,related accept",
        "    ct state invalid drop",
        # IPv6 needs ICMPv6 for neighbour discovery and router advertisements,
        # and IPv4 ping is useful for field diagnosis.
        "    meta l4proto ipv6-icmp accept",
        "    meta l4proto icmp icmp type echo-request accept",
    ]
    if tcp:
        lines.append(f"    tcp dport {_port_list(tcp)} accept")
    if udp:
        lines.append(f"    udp dport {_port_list(udp)} accept")
    lines += ["  }", "}", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--esphome", action="store_true")
    parser.add_argument("--airplay", action="store_true")
    parser.add_argument("--ptp", action="store_true")
    parser.add_argument("--ssh", action="store_true")
    parser.add_argument("--adb-tcp", action="store_true")
    parser.add_argument("--recovery-ap", action="store_true")
    parser.add_argument("--output", type=Path, help="write here instead of stdout")
    args = parser.parse_args(argv)
    features = Features(
        esphome=args.esphome,
        airplay=args.airplay,
        ptp=args.ptp,
        ssh=args.ssh,
        adb_tcp=args.adb_tcp,
        recovery_ap=args.recovery_ap,
    )
    text = render_ruleset(features)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

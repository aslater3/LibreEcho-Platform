#!/usr/bin/env python3
"""Dispatch the reviewed DTB semantics; both parity targets use Radar's DTB."""
from __future__ import annotations

import argparse
from pathlib import Path

from target_registry import add_target_arguments, validate_target_arguments, get_target
from verify_radar_puffin_dtb import ContractError, verify_dtb

DTB_VERIFIERS = {'radar_puffin': verify_dtb}


def verify_target_dtb(target: str, dtb: Path) -> None:
    profile = get_target(target)['dtb_verifier']
    DTB_VERIFIERS[profile](dtb)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dtb', type=Path, required=True)
    add_target_arguments(parser)
    args = parser.parse_args()
    validate_target_arguments(parser, args)
    try:
        verify_target_dtb(args.target, args.dtb)
    except (ContractError, OSError) as error:
        parser.exit(1, f'ERROR: {args.target} DTB contract failed: {error}\n')
    print(f'target_dtb_hardware_contract=PASS target={args.target} verifier={get_target(args.target)["dtb_verifier"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

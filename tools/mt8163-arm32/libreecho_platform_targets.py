#!/usr/bin/env python3
"""Platform-owned parity target table; Product owns full release descriptors."""
from __future__ import annotations

import argparse
import os
import re

DEFAULT_TARGET = 'radar_puffin'
TARGETS = {
    'radar_puffin': {
        'release_slug': 'radar-puffin', 'hw_profile': 'radar_puffin@1',
        'fastboot_products': ('RADAR',), 'dtb_verifier': 'radar_puffin',
    },
    'biscuit': {
        'release_slug': 'biscuit', 'hw_profile': 'biscuit@0',
        'fastboot_products': ('BISCUIT',), 'dtb_verifier': 'radar_puffin',
    },
}


def get_target(target: str) -> dict:
    if target not in TARGETS:
        raise ValueError(f'unknown target: {target!r}')
    return TARGETS[target]


def descriptor_digest(value: str | None) -> str | None:
    if value is not None and re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise ValueError('target descriptor digest must be 64 lowercase hex characters')
    return value


def identity_bytes(target: str, digest: str | None = None) -> bytes:
    record = get_target(target)
    digest = descriptor_digest(digest)
    text = (f'target_id={target}\nrelease_slug={record["release_slug"]}\n'
            f'hw_profile={record["hw_profile"]}\n')
    if digest is not None:
        text += f'descriptor_sha256={digest}\n'
    return text.encode('ascii')


def add_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--target', default=os.environ.get('LIBREECHO_TARGET', DEFAULT_TARGET),
                        help='target ID (default: LIBREECHO_TARGET or radar_puffin)')
    parser.add_argument('--target-descriptor-sha256', type=descriptor_digest,
                        help='canonical Product target descriptor SHA256')


def validate_target_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    try:
        get_target(args.target)
        descriptor_digest(args.target_descriptor_sha256)
    except ValueError as error:
        parser.error(str(error))

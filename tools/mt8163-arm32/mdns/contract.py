#!/usr/bin/env python3
"""Load and validate the checked-in shared mDNS runtime contract.

The contract is the single source of truth for the loader, libraries,
executables, configuration, accounts and license records a boot-contained
discovery runtime must contain.  Provenance and corresponding-source licensing
stay in the product build wiring; this file only fixes the ABI and the required
runtime inventory so the builder and the independent verifier fail closed on the
same inputs.
"""
import json
from pathlib import Path

SCHEMA = 'libreecho-mdns-runtime-contract/v1'
HERE = Path(__file__).resolve().parent
CONTRACT_PATH = HERE / 'runtime-contract.json'
CATEGORIES = ('executables', 'libraries', 'config', 'accounts', 'licenses')


def _relative(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError('contract ' + label + ' entry is not a path')
    path = Path(value)
    if path.is_absolute() or value.startswith('/') or '..' in path.parts:
        raise ValueError('contract ' + label + ' entry is not runtime-relative')
    return value


def validate(document):
    if not isinstance(document, dict) or document.get('schema') != SCHEMA:
        raise ValueError('unsupported mdns runtime contract')
    for key in ('image_runtime_root', 'image_marker', 'init_wrapper'):
        _relative(document.get(key), key)
    abi = document.get('abi')
    if not isinstance(abi, dict):
        raise ValueError('contract abi record missing')
    for key, value in (('class', 'ELF32'), ('machine', 'ARM'), ('float', 'hard-float')):
        if abi.get(key) != value:
            raise ValueError('contract abi mismatch: ' + key)
    if not isinstance(abi.get('interpreter'), str) or not abi['interpreter'].startswith('/lib/'):
        raise ValueError('contract interpreter is invalid')
    loader = _relative(document.get('loader'), 'loader')
    if not loader.startswith('lib/'):
        raise ValueError('contract loader is not a runtime loader path')
    for category in CATEGORIES:
        entries = document.get(category)
        if not isinstance(entries, list) or not entries:
            raise ValueError('contract category missing: ' + category)
        for entry in entries:
            _relative(entry, category)
    packages = document.get('packages')
    if not isinstance(packages, list) or not packages:
        raise ValueError('contract package set missing')
    for name in packages:
        if not isinstance(name, str) or not name:
            raise ValueError('contract package entry is invalid')
    runtime_dirs = document.get('runtime_dirs')
    if not isinstance(runtime_dirs, dict):
        raise ValueError('contract runtime directories missing')
    for key in ('state_root', 'bus', 'services', 'control_socket', 'pidfile'):
        value = runtime_dirs.get(key)
        if not isinstance(value, str) or not value.startswith('/'):
            raise ValueError('contract runtime directory is invalid: ' + key)
    if not runtime_dirs['bus'].startswith(runtime_dirs['state_root'] + '/'):
        raise ValueError('contract bus directory is outside the shared runtime')
    return document


def load(path=CONTRACT_PATH):
    try:
        document = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError('mdns runtime contract is unreadable: ' + str(path)) from exc
    return validate(document)


def required_paths(document):
    paths = {document['loader']}
    for category in CATEGORIES:
        paths.update(document[category])
    return paths


def category_paths(document, category):
    if category not in CATEGORIES:
        raise ValueError('unknown contract category: ' + category)
    return set(document[category])

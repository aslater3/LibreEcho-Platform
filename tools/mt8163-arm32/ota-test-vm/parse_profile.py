"""Validate captured state before mkdisk allocates any disk or loop device."""
import json
from pathlib import Path
import sys


def parse_profile(path):
    profile = json.loads(Path(path).read_text())
    if not isinstance(profile, dict) or not isinstance(profile.get('config_export'), dict):
        raise ValueError('profile requires object-valued config_export')
    state = profile.get('system_update')
    if not isinstance(state, dict):
        raise ValueError('profile requires object-valued system_update')
    slot = state.get('current_slot')
    rollback = state.get('rollback_available')
    if slot not in ('a', 'b') or not isinstance(rollback, bool):
        raise ValueError('system_update requires current_slot a/b and boolean rollback_available')
    return profile['config_export'], slot, rollback


if __name__ == '__main__':
    try:
        config, slot, rollback = parse_profile(sys.argv[1])
        Path(sys.argv[2]).write_text(json.dumps(config, indent=2, sort_keys=True) + '\n')
        Path(sys.argv[3]).write_text(slot + '\n' + str(rollback).lower() + '\n')
    except (OSError, ValueError, IndexError) as exc:
        sys.exit('invalid profile: ' + str(exc))

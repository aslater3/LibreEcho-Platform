#!/usr/bin/env python3
"""Platform-owned parity target table; Product owns full release descriptors."""
from __future__ import annotations

import argparse
import os
import re

DEFAULT_TARGET = 'radar_puffin'

# Per-target speaker audio policy.  Adding a hardware model is a table entry:
# the image build renders it to /etc/libreecho/audio-profile, the audio engine
# follows it at start-up (chain, codec profile, analogue gain, digital gains),
# and the kernel only exposes the generic controls it names.
#   speaker_chain  engine DSP chain: radar_puffin (two-way) | biscuit (one driver)
#   codec_profile  kernel "Speaker Codec Profile": Radar (crossover) | Flat
#   hp_driver_gain codec "HP Driver Gain Volume" index (0..35, 0 = 0 dB)
#   pre_gain_db    digital gain before the chain (chain limiter holds the ceiling)
#   bass_makeup_db post-compressor makeup on the chain's bass bands
AUDIO_CHAINS = ('radar_puffin', 'biscuit')
CODEC_PROFILES = ('Radar', 'Flat')
AUDIO_KEYS = ('speaker_chain', 'codec_profile', 'hp_driver_gain',
              'pre_gain_db', 'bass_makeup_db')
TARGETS = {
    'radar_puffin': {
        'release_slug': 'radar-puffin', 'hw_profile': 'radar_puffin@1',
        'fastboot_products': ('RADAR',), 'dtb_verifier': 'radar_puffin',
        'audio': {'speaker_chain': 'radar_puffin', 'codec_profile': 'Radar',
                  'hp_driver_gain': 6, 'pre_gain_db': 0.0, 'bass_makeup_db': 0.0},
    },
    'biscuit': {
        'release_slug': 'biscuit', 'hw_profile': 'biscuit@0',
        'fastboot_products': ('BISCUIT',), 'dtb_verifier': 'radar_puffin',
        # Owner listening on hardware: flat codec biquads restore the highs
        # the Radar crossover removed; +6 dB pre-gain and +2.5 dB bass makeup.
        'audio': {'speaker_chain': 'biscuit', 'codec_profile': 'Flat',
                  'hp_driver_gain': 6, 'pre_gain_db': 6.0, 'bass_makeup_db': 2.5},
    },
}


def validate_audio(audio: dict) -> dict:
    if set(audio) != set(AUDIO_KEYS):
        raise ValueError(f'audio profile keys must be exactly {AUDIO_KEYS}')
    if audio['speaker_chain'] not in AUDIO_CHAINS:
        raise ValueError(f'unknown speaker_chain: {audio["speaker_chain"]!r}')
    if audio['codec_profile'] not in CODEC_PROFILES:
        raise ValueError(f'unknown codec_profile: {audio["codec_profile"]!r}')
    gain = audio['hp_driver_gain']
    if not isinstance(gain, int) or isinstance(gain, bool) or not 0 <= gain <= 35:
        raise ValueError('hp_driver_gain must be an integer 0..35')
    for key, low, high in (('pre_gain_db', 0.0, 12.0), ('bass_makeup_db', 0.0, 6.0)):
        value = audio[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not low <= value <= high:
            raise ValueError(f'{key} must be within {low}..{high} dB')
        if audio['speaker_chain'] == 'radar_puffin' and value != 0:
            raise ValueError(f'{key} is only supported on the biscuit chain')
    return audio


def audio_profile_bytes(target: str) -> bytes:
    """Deterministic key=value rendering consumed by the audio engine."""
    audio = validate_audio(get_target(target)['audio'])
    text = 'schema=1\n' + f'target_id={target}\n'
    for key in AUDIO_KEYS:
        value = audio[key]
        text += f'{key}={value:.2f}\n' if isinstance(value, float) else f'{key}={value}\n'
    return text.encode('ascii')


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

#!/usr/bin/env python3
"""Capture the Echo microphone transport as a nine-channel 24-bit WAV."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import wave


RATE = 16000
CHANNELS = 9
SAMPLE_BYTES = 3
FRAME_BYTES = CHANNELS * SAMPLE_BYTES
DEFAULT_SERIAL = "G2A0RF0485020316"
REMOTE_CAPTURE = "/tmp/libreecho-array-capture.wav"
REMOTE_COUNTDOWN = "/tmp/libreecho-array-countdown.pcm"
COUNTDOWN_RATE = 48000
COUNTDOWN_CHANNELS = 2
COUNTDOWN_SECONDS = 3
COUNTDOWN_TONE_SECONDS = 0.12
COUNTDOWN_FREQUENCY_HZ = 880.0
COUNTDOWN_AMPLITUDE = 6000


def run(command: list[str], *, env: dict[str, str] | None = None,
        capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        env=env,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
    )


def adb(serial: str, *arguments: str,
        capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    return run(
        [os.environ.get("ADB_BIN", "adb"), "-s", serial, *arguments],
        capture_output=capture_output,
    )


def root_control(serial: str, action: str, script_dir: Path,
                 timeout: int = 20) -> None:
    adb_bin = os.environ.get("ADB_BIN", "adb")
    helper = script_dir.parent / "adb-run-root.sh"
    controller = script_dir / "array_capture_control_root.sh"
    if not helper.is_file() or not controller.is_file():
        raise RuntimeError("array capture control helpers are missing")
    with tempfile.NamedTemporaryFile("w", encoding="ascii") as action_file:
        action_file.write(action + "\n")
        action_file.flush()
        adb(serial, "push", action_file.name,
            "/tmp/libreecho-array-capture.action")
    environment = os.environ.copy()
    environment["ADB_SERIAL"] = serial
    environment["ADB_BIN"] = adb_bin
    run([str(helper), str(controller), str(timeout)], env=environment)


def countdown_pcm() -> bytes:
    frames = COUNTDOWN_RATE * COUNTDOWN_SECONDS
    tone_frames = round(COUNTDOWN_RATE * COUNTDOWN_TONE_SECONDS)
    output = bytearray(frames * COUNTDOWN_CHANNELS * 2)
    for second in range(COUNTDOWN_SECONDS):
        start = second * COUNTDOWN_RATE
        for offset in range(tone_frames):
            # A short cosine fade avoids a click at each boundary.
            fade_frames = min(tone_frames // 4, 240)
            fade = 1.0
            if offset < fade_frames:
                fade = offset / fade_frames
            elif offset >= tone_frames - fade_frames:
                fade = (tone_frames - 1 - offset) / fade_frames
            sample = int(
                COUNTDOWN_AMPLITUDE * fade *
                math.sin(
                    2.0 * math.pi * COUNTDOWN_FREQUENCY_HZ *
                    offset / COUNTDOWN_RATE
                )
            )
            frame = start + offset
            struct.pack_into("<hh", output, frame * 4, sample, sample)
    return bytes(output)


def stage_countdown(serial: str) -> str:
    payload = countdown_pcm()
    with tempfile.NamedTemporaryFile("wb") as tone_file:
        tone_file.write(payload)
        tone_file.flush()
        adb(serial, "push", tone_file.name, REMOTE_COUNTDOWN)
    return hashlib.sha256(payload).hexdigest()


def read_device_text(serial: str, path: str) -> str:
    result = adb(
        serial, "exec-out", "sh", "-c",
        f"tr -d '\\000\\r\\n ' < {path} 2>/dev/null",
        capture_output=True,
    )
    return result.stdout.strip()


def read_miccal(serial: str) -> list[int]:
    values: list[int] = []
    for index in range(7):
        text = read_device_text(serial, f"/proc/idme/miccal.{index}")
        try:
            value = int(text)
        except ValueError:
            value = 16384
        if not 0 < value <= 65535:
            value = 16384
        values.append(value)
    return values


def validate_capture(path: Path, duration: int) -> dict:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.getnframes()
    if (channels, sample_width, rate) != (CHANNELS, SAMPLE_BYTES, RATE):
        raise RuntimeError(
            "unexpected capture format: "
            f"{channels} channels, {sample_width * 8} bits, {rate} Hz")
    expected_frames = duration * RATE
    if frames != expected_frames:
        raise RuntimeError(
            f"short microphone capture: {frames}/{expected_frames} frames")
    return {
        "frames": frames,
        "pcm_bytes": frames * FRAME_BYTES,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_label(value: str) -> str:
    cleaned = "".join(
        character.lower() if character.isalnum() else "-"
        for character in value
    )
    return "-".join(part for part in cleaned.split("-") if part) or "capture"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Pause wake detection, capture all nine raw microphone channels "
            "to the host, then restore wake detection."
        )
    )
    parser.add_argument("--serial", default=os.environ.get(
        "ADB_SERIAL", DEFAULT_SERIAL))
    parser.add_argument("--label", required=True)
    parser.add_argument("--distance-m", type=float, required=True)
    parser.add_argument(
        "--azimuth-deg", type=float, required=True,
        help="Clockwise source bearing in degrees; 0 is the marked front.")
    parser.add_argument("--duration", type=float, default=6.0)
    parser.add_argument("--lead-seconds", type=int, default=3)
    parser.add_argument(
        "--countdown-tones", action="store_true",
        help=(
            "Start capture first, then play three short tones one second "
            "apart through the system audio bus."
        ),
    )
    parser.add_argument("--phrase", default="Alexa")
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path("array-captures"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if (args.duration < 1 or args.duration > 30 or
            not args.duration.is_integer()):
        raise SystemExit("--duration must be a whole number from 1 to 30")
    duration = int(args.duration)
    if args.lead_seconds < 0 or args.lead_seconds > 30:
        raise SystemExit("--lead-seconds must be between 0 and 30")
    if args.distance_m <= 0:
        raise SystemExit("--distance-m must be greater than 0")

    script_dir = Path(__file__).resolve().parent
    args.output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now(dt.timezone.utc)
    stem = (
        f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}-"
        f"{safe_label(args.label)}"
    )
    wav_path = args.output_dir / f"{stem}.wav"
    metadata_path = args.output_dir / f"{stem}.json"

    state = adb(args.serial, "get-state", capture_output=True).stdout.strip()
    if state != "device":
        raise RuntimeError(
            f"ADB device {args.serial} is not ready (state={state!r})")
    miccal = read_miccal(args.serial)
    countdown_sha256 = None
    if args.countdown_tones:
        if duration < 8:
            raise SystemExit(
                "--countdown-tones requires a duration of at least 8 seconds")
        countdown_sha256 = stage_countdown(args.serial)
    control_active = False
    try:
        print(
            f"Position yourself {args.distance_m:g} m from azimuth "
            f"{args.azimuth_deg:g}° and "
            + (
                f"say “{args.phrase}” three times after the third tone."
                if args.countdown_tones else
                f"say “{args.phrase}” clearly once."
            ),
            flush=True,
        )
        for remaining in range(args.lead_seconds, 0, -1):
            print(f"Recording in {remaining}…", flush=True)
            time.sleep(1)
        print(
            f"RECORDING for {duration:g} seconds"
            + (
                " — wait for three tones, then say "
                f"“{args.phrase}” three times."
                if args.countdown_tones else
                f" — say “{args.phrase}” now."
            ),
            flush=True,
        )
        started = time.monotonic()
        control_active = True
        countdown_mode = "tones" if args.countdown_tones else "none"
        root_control(
            args.serial,
            f"capture {duration} {countdown_mode}",
            script_dir,
            timeout=duration + 20,
        )
        adb(args.serial, "pull", REMOTE_CAPTURE, str(wav_path))
        capture = validate_capture(wav_path, duration)
        capture["capture_elapsed_seconds"] = time.monotonic() - started
        print("Recording complete.", flush=True)
    finally:
        if control_active:
            root_control(args.serial, "resume", script_dir)

    metadata = {
        "schema": 1,
        "captured_at_utc": timestamp.isoformat(),
        "device_serial": args.serial,
        "label": args.label,
        "phrase": args.phrase,
        "distance_m": args.distance_m,
        "azimuth_degrees_clockwise_from_front": args.azimuth_deg % 360.0,
        "sample_rate_hz": RATE,
        "channels": CHANNELS,
        "container_bits": 24,
        "valid_bits": 16,
        "encoding": "pcm_s24_3le_left_aligned_s16",
        "idme_miccal_q14": miccal,
        "idme_calibration_mapping": "raw_identity_provisional",
        "idme_calibration_mode": "direct_q14_provisional",
        "countdown": {
            "enabled": args.countdown_tones,
            "tone_count": 3 if args.countdown_tones else 0,
            "tone_onsets_seconds": [0.0, 1.0, 2.0]
            if args.countdown_tones else [],
            "tone_duration_seconds": COUNTDOWN_TONE_SECONDS
            if args.countdown_tones else 0.0,
            "speech_search_start_seconds": 3.0
            if args.countdown_tones else 0.0,
            "pcm_sha256": countdown_sha256,
        },
        "wav_file": wav_path.name,
        "wav_sha256": sha256_file(wav_path),
        **capture,
    }
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"WAV: {wav_path}")
    print(f"Metadata: {metadata_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, OSError,
            json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)

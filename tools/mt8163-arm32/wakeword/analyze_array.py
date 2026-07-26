#!/usr/bin/env python3
"""Measure Echo array levels, polarity, timing, calibration, and geometry."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import sys
import wave

import numpy as np
from scipy.signal import lfilter, stft


ACTIVE_CHANNELS = 7
MICCAL_UNITY = 16384.0
SPEED_OF_SOUND_M_S = 343.0
MAX_LAG_SAMPLES = 32


def read_wav(path: Path) -> tuple[int, np.ndarray]:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.getnframes()
        payload = wav.readframes(frames)
    if channels != 9 or sample_width != 3 or rate != 16000:
        raise ValueError(
            f"{path}: expected 9-channel 24-bit 16 kHz WAV, got "
            f"{channels} channels, {sample_width * 8} bits, {rate} Hz")
    packed = np.frombuffer(payload, dtype=np.uint8)
    expected = frames * channels * sample_width
    if packed.size != expected:
        raise ValueError(f"{path}: truncated PCM payload")
    packed = packed.reshape(frames, channels, sample_width).astype(np.int32)
    values = packed[:, :, 0]
    values |= packed[:, :, 1] << 8
    values |= packed[:, :, 2] << 16
    values = np.where(values & 0x800000, values - 0x1000000, values)
    # The board transports signed 16-bit samples left-aligned in S24_3LE.
    return rate, np.trunc(values / 256.0).astype(np.float64)


def load_metadata(path: Path) -> dict:
    metadata_path = path.with_suffix(".json")
    if not metadata_path.is_file():
        raise ValueError(f"{path}: sidecar metadata is missing")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("wav_file") not in (None, path.name):
        raise ValueError(f"{metadata_path}: wav_file does not match")
    return metadata


def frame_rms(samples: np.ndarray, frame: int = 320,
              hop: int = 160) -> np.ndarray:
    if samples.shape[0] < frame:
        return np.array([], dtype=np.float64)
    count = 1 + (samples.shape[0] - frame) // hop
    output = np.empty(count, dtype=np.float64)
    for index in range(count):
        block = samples[index * hop:index * hop + frame]
        output[index] = math.sqrt(float(np.mean(block * block)))
    return output


def highpass_80hz(samples: np.ndarray) -> np.ndarray:
    coefficient = 0.9691
    return lfilter(
        [coefficient, -coefficient],
        [1.0, -coefficient],
        samples,
        axis=0,
    )


def detect_countdown(samples: np.ndarray, rate: int) -> dict | None:
    frequencies, times, spectrum = stft(
        samples[:, :ACTIVE_CHANNELS],
        fs=rate,
        nperseg=320,
        noverlap=240,
        axis=0,
        boundary=None,
        padded=False,
    )
    power = np.abs(spectrum) ** 2
    tone_power = power[
        (frequencies >= 820) & (frequencies <= 940)
    ].sum(axis=0)
    speech_band_power = power[
        (frequencies >= 300) & (frequencies <= 3000)
    ].sum(axis=0)
    ratio = np.median(
        tone_power / (speech_band_power + 1e-12),
        axis=0,
    )
    active = np.flatnonzero(ratio >= 0.70)
    if active.size == 0:
        return None
    groups: list[tuple[int, int]] = []
    first = previous = int(active[0])
    for value in active[1:]:
        current = int(value)
        if current > previous + 1:
            groups.append((first, previous))
            first = current
        previous = current
    groups.append((first, previous))
    # Ignore narrow incidental tonal frames. The generated cue produces
    # approximately 24 active 5 ms hops per tone.
    groups = [
        group for group in groups
        if group[1] - group[0] + 1 >= 8
    ]
    if len(groups) < 3:
        return None
    groups = groups[-3:]
    half_window = 0.01
    onsets = [
        max(0.0, float(times[first]) - half_window)
        for first, _ in groups
    ]
    ends = [
        float(times[last]) + half_window
        for _, last in groups
    ]
    return {
        "tone_onsets_seconds": onsets,
        "tone_ends_seconds": ends,
        "speech_search_start_seconds": ends[-1] + 0.20,
        "maximum_tone_band_ratio": float(np.max(ratio)),
    }


def speech_bounds(samples: np.ndarray, rate: int,
                  search_start: int = 0) -> tuple[int, int, float]:
    search_start = max(0, min(search_start, samples.shape[0]))
    searched = samples[search_start:]
    channel_energies = [
        frame_rms(searched[:, channel])
        for channel in range(ACTIVE_CHANNELS)
    ]
    if not channel_energies or channel_energies[0].size == 0:
        raise ValueError("capture is too short to analyse")
    # The 75th percentile remains sensitive when one or two array lanes are
    # weak without allowing a single noisy lane to define speech activity.
    energies = np.percentile(
        np.stack(channel_energies, axis=1), 75, axis=1)
    noise = float(np.percentile(energies, 20))
    peak = float(np.max(energies))
    threshold = max(noise * 2.5, peak * 0.18, 8.0)
    active = np.flatnonzero(energies >= threshold)
    if active.size == 0:
        centre = int(np.argmax(energies))
        first = max(0, centre - 25)
        last = min(energies.size - 1, centre + 25)
    else:
        peak_index = int(np.argmax(energies))
        # A calibration take contains three repetitions at a normal pace.
        # Keep active frames within five seconds of the strongest phrase so
        # all repetitions contribute to timing and level estimates.
        nearby = active[np.abs(active - peak_index) <= 500]
        first = int(nearby[0])
        last = int(nearby[-1])
    padding = round(0.15 * rate)
    start = max(search_start, search_start + first * 160 - padding)
    end = min(
        samples.shape[0],
        search_start + last * 160 + 320 + padding,
    )
    return start, end, threshold


def rms(values: np.ndarray, axis: int | None = None) -> np.ndarray | float:
    return np.sqrt(np.mean(values * values, axis=axis))


def regions(samples: np.ndarray, start: int, end: int,
            noise_start: int = 0) -> tuple[np.ndarray, np.ndarray]:
    speech = samples[start:end]
    noise_start = max(0, min(noise_start, start))
    before = samples[noise_start:max(noise_start, start - 320)]
    after = samples[min(samples.shape[0], end + 320):]
    if before.size and after.size:
        noise = np.concatenate((before, after), axis=0)
    elif before.size:
        noise = before
    elif after.size:
        noise = after
    else:
        noise = samples[:max(1, samples.shape[0] // 5)]
    return speech, noise


def snr_db(speech_rms: np.ndarray | float,
           noise_rms: np.ndarray | float) -> np.ndarray | float:
    speech_power = np.maximum(
        np.asarray(speech_rms, dtype=np.float64) ** 2 -
        np.asarray(noise_rms, dtype=np.float64) ** 2,
        1e-9,
    )
    noise_power = np.maximum(
        np.asarray(noise_rms, dtype=np.float64) ** 2, 1e-9)
    result = 10.0 * np.log10(speech_power / noise_power)
    return float(result) if result.ndim == 0 else result


def apply_calibration(samples: np.ndarray, miccal: np.ndarray,
                      mode: str) -> np.ndarray:
    gains = np.ones(samples.shape[1], dtype=np.float64)
    if mode == "direct":
        gains[:ACTIVE_CHANNELS] = miccal / MICCAL_UNITY
    elif mode == "inverse":
        gains[:ACTIVE_CHANNELS] = MICCAL_UNITY / miccal
    elif mode != "off":
        raise ValueError(f"unknown calibration mode: {mode}")
    return samples * gains


def lag_correlation(reference: np.ndarray, channel: np.ndarray,
                    maximum: int = MAX_LAG_SAMPLES) -> tuple[int, float]:
    reference = np.diff(reference)
    channel = np.diff(channel)
    best_lag = 0
    best_correlation = 0.0
    for lag in range(-maximum, maximum + 1):
        if lag > 0:
            left = reference[:-lag]
            right = channel[lag:]
        elif lag < 0:
            left = reference[-lag:]
            right = channel[:lag]
        else:
            left = reference
            right = channel
        left = left - np.mean(left)
        right = right - np.mean(right)
        denominator = math.sqrt(
            float(np.dot(left, left) * np.dot(right, right)))
        correlation = (
            float(np.dot(left, right)) / denominator
            if denominator > 0 else 0.0
        )
        if abs(correlation) > abs(best_correlation):
            best_lag = lag
            best_correlation = correlation
    return best_lag, best_correlation


def shift_to_reference(values: np.ndarray, lag: int) -> np.ndarray:
    output = np.zeros_like(values)
    if lag > 0:
        output[:-lag] = values[lag:]
    elif lag < 0:
        output[-lag:] = values[:lag]
    else:
        output[:] = values
    return output


def mix_metrics(values: np.ndarray, start: int, end: int,
                noise_start: int = 0) -> tuple[float, float, float]:
    speech, noise = regions(
        values[:, None], start, end, noise_start=noise_start)
    speech_value = float(rms(speech[:, 0]))
    noise_value = float(rms(noise[:, 0]))
    return speech_value, noise_value, float(
        snr_db(speech_value, noise_value))


def analyse_capture(path: Path) -> dict:
    rate, raw = read_wav(path)
    filtered = highpass_80hz(raw)
    metadata = load_metadata(path)
    miccal = np.asarray(
        metadata.get("idme_miccal_q14", [16384] * ACTIVE_CHANNELS),
        dtype=np.float64,
    )
    if miccal.shape != (ACTIVE_CHANNELS,) or np.any(miccal <= 0):
        raise ValueError(f"{path}: invalid IDME calibration vector")
    configured_search_start_seconds = float(
        metadata.get("countdown", {}).get(
            "speech_search_start_seconds", 0.0))
    observed_countdown = None
    search_start_seconds = configured_search_start_seconds
    if metadata.get("countdown", {}).get("enabled"):
        observed_countdown = detect_countdown(filtered, rate)
        if observed_countdown:
            search_start_seconds = max(
                search_start_seconds,
                float(observed_countdown[
                    "speech_search_start_seconds"]),
            )
    search_start = round(search_start_seconds * rate)
    start, end, threshold = speech_bounds(
        filtered, rate, search_start=search_start)
    speech, noise = regions(
        filtered, start, end, noise_start=search_start)
    speech_levels = np.asarray(rms(speech, axis=0))
    noise_levels = np.asarray(rms(noise, axis=0))
    channel_snr = np.asarray(snr_db(speech_levels, noise_levels))

    reference = int(np.argmax(channel_snr[:ACTIVE_CHANNELS]))
    lags = np.zeros(ACTIVE_CHANNELS, dtype=np.int32)
    correlations = np.ones(ACTIVE_CHANNELS, dtype=np.float64)
    for channel in range(ACTIVE_CHANNELS):
        lags[channel], correlations[channel] = lag_correlation(
            speech[:, reference], speech[:, channel])
    polarity = np.where(correlations < 0, -1, 1)

    direct = apply_calibration(filtered, miccal, "direct")
    current_mix = np.mean(direct[:, [0, 1, 3, 4]], axis=1)
    measured_delay_sum = np.mean(np.stack((
        direct[:, 0],
        shift_to_reference(direct[:, 3], 4),
    ), axis=1), axis=1)
    best_single = direct[:, reference]
    aligned_channels = []
    included_channels = []
    for channel in range(ACTIVE_CHANNELS):
        if abs(correlations[channel]) < 0.12:
            continue
        aligned_channels.append(
            shift_to_reference(
                direct[:, channel] * polarity[channel],
                int(lags[channel]),
            )
        )
        included_channels.append(channel)
    aligned_mix = (
        np.mean(np.stack(aligned_channels, axis=1), axis=1)
        if aligned_channels else best_single.copy()
    )

    modes = {}
    for mode in ("off", "direct", "inverse"):
        calibrated = apply_calibration(filtered, miccal, mode)
        levels = np.asarray(rms(calibrated[start:end, :ACTIVE_CHANNELS],
                                axis=0))
        modes[mode] = {
            "speech_rms": levels.tolist(),
            "channel_cv": float(np.std(levels) / max(np.mean(levels), 1e-9)),
        }

    channel_metrics = []
    peaks = np.max(np.abs(raw), axis=0)
    for channel in range(raw.shape[1]):
        item = {
            "channel": channel,
            "active_microphone_candidate": channel < ACTIVE_CHANNELS,
            "speech_rms": float(speech_levels[channel]),
            "noise_rms": float(noise_levels[channel]),
            "snr_db": float(channel_snr[channel]),
            "peak": float(peaks[channel]),
            "clipped_percent": float(
                100.0 * np.mean(np.abs(raw[:, channel]) >= 32760)),
        }
        if channel < ACTIVE_CHANNELS:
            item.update({
                "lag_samples_from_reference": int(lags[channel]),
                "lag_microseconds_from_reference": float(
                    1e6 * lags[channel] / rate),
                "correlation": float(correlations[channel]),
                "suggested_polarity": int(polarity[channel]),
                "idme_miccal_q14": int(miccal[channel]),
            })
        channel_metrics.append(item)

    comparisons = {}
    for name, values in (
        ("best_single", best_single),
        ("current_direct_q14_mix_0_1_3_4", current_mix),
        ("measured_delay_sum_direct_q14_0_3", measured_delay_sum),
        ("polarity_timing_aligned_direct_q14_mix", aligned_mix),
    ):
        speech_rms, noise_rms, signal_snr = mix_metrics(
            values, start, end, noise_start=search_start)
        comparisons[name] = {
            "speech_rms": speech_rms,
            "noise_rms": noise_rms,
            "snr_db": signal_snr,
        }

    return {
        "wav": str(path),
        "metadata": metadata,
        "speech_region": {
            "configured_search_start_seconds":
                configured_search_start_seconds,
            "effective_search_start_seconds": search_start_seconds,
            "observed_countdown": observed_countdown,
            "start_sample": start,
            "end_sample": end,
            "start_seconds": start / rate,
            "end_seconds": end / rate,
            "detector_rms_threshold": threshold,
        },
        "reference_channel": reference,
        "included_in_aligned_mix": included_channels,
        "channels": channel_metrics,
        "calibration_comparison": modes,
        "mix_comparison": comparisons,
    }


def fit_geometry(captures: list[dict]) -> dict | None:
    strongest_by_azimuth: dict[float, dict] = {}
    for capture in captures:
        azimuth = capture["metadata"].get(
            "azimuth_degrees_clockwise_from_front")
        if azimuth is None:
            continue
        angle = float(azimuth) % 360.0
        incumbent = strongest_by_azimuth.get(angle)
        quality = float(capture["channels"][0]["snr_db"])
        if (incumbent is None or quality >
                float(incumbent["channels"][0]["snr_db"])):
            strongest_by_azimuth[angle] = capture
    selected = list(strongest_by_azimuth.values())
    if len(selected) < 3:
        return None

    rows: list[list[float]] = []
    reference_votes: list[int] = []
    for capture in selected:
        metadata = capture["metadata"]
        angle = math.radians(float(
            metadata["azimuth_degrees_clockwise_from_front"]))
        # The intercept separates a fixed lane/transport delay from the
        # direction-dependent acoustic arrival time.
        rows.append([1.0, math.sin(angle), math.cos(angle)])
        reference_votes.append(int(capture["reference_channel"]))
    if np.linalg.matrix_rank(np.asarray(rows, dtype=np.float64)) < 3:
        return None
    # Re-express every capture relative to one session-wide reference.
    reference = max(set(reference_votes), key=reference_votes.count)
    matrix = np.asarray(rows, dtype=np.float64)
    fitted = []
    for channel in range(ACTIVE_CHANNELS):
        relative_lags = []
        weights = []
        for capture in selected:
            metrics = capture["channels"]
            lag = (
                float(metrics[channel]["lag_samples_from_reference"]) -
                float(metrics[reference]["lag_samples_from_reference"])
            )
            relative_lags.append(lag)
            weights.append(min(
                abs(float(metrics[channel]["correlation"])),
                abs(float(metrics[reference]["correlation"])),
            ))
        lag_values = np.asarray(relative_lags, dtype=np.float64)
        weight_values = np.maximum(np.asarray(weights), 0.05)
        weighted_matrix = matrix * np.sqrt(weight_values[:, None])
        weighted_target = lag_values * np.sqrt(weight_values)
        coefficients, _, _, _ = np.linalg.lstsq(
            weighted_matrix, weighted_target, rcond=None)
        position = (
            -coefficients[1:] * SPEED_OF_SOUND_M_S / 16000.0)
        predicted_lag = matrix @ coefficients
        residual = lag_values - predicted_lag
        fitted.append({
            "channel": channel,
            "transport_bias_samples": float(coefficients[0]),
            "x_right_mm": float(position[0] * 1000.0),
            "y_front_mm": float(position[1] * 1000.0),
            "radius_from_reference_mm": float(
                np.linalg.norm(position) * 1000.0),
            "fit_rms_samples": float(math.sqrt(np.mean(residual ** 2))),
        })
    return {
        "coordinate_system": (
            "relative to reference; +x right, +y marked front; source "
            "azimuth clockwise from front"
        ),
        "reference_channel": reference,
        "speed_of_sound_m_s": SPEED_OF_SOUND_M_S,
        "capture_count": len(selected),
        "captures_used": [capture["wav"] for capture in selected],
        "warning": (
            "Three measured azimuths determine transport bias and planar "
            "position exactly; a rear or diagonal take is still required "
            "to validate the inferred geometry."
        ),
        "positions": fitted,
    }


def session_calibration(captures: list[dict]) -> dict:
    result = {}
    for mode in ("off", "direct", "inverse"):
        levels = np.asarray([
            capture["calibration_comparison"][mode]["speech_rms"]
            for capture in captures
        ], dtype=np.float64)
        per_channel = np.median(levels, axis=0)
        result[mode] = {
            "median_speech_rms_by_channel": per_channel.tolist(),
            "channel_cv": float(
                np.std(per_channel) / max(np.mean(per_channel), 1e-9)),
        }
    suggestion = min(result, key=lambda mode: result[mode]["channel_cv"])
    return {
        "modes": result,
        "lowest_channel_spread_mode": suggestion,
        "warning": (
            "Treat this as evidence, not proof: directional microphone "
            "response and source placement also affect channel levels."
        ),
    }


def session_beamforming(captures: list[dict]) -> dict:
    names = (
        "best_single",
        "current_direct_q14_mix_0_1_3_4",
        "measured_delay_sum_direct_q14_0_3",
        "polarity_timing_aligned_direct_q14_mix",
    )
    comparisons = {}
    for name in names:
        scores = [
            float(capture["mix_comparison"][name]["snr_db"])
            for capture in captures
        ]
        comparisons[name] = {
            "snr_db_by_capture": scores,
            "mean_snr_db": float(np.mean(scores)),
            "minimum_snr_db": float(np.min(scores)),
        }
    baseline = comparisons["current_direct_q14_mix_0_1_3_4"]
    measured = comparisons["measured_delay_sum_direct_q14_0_3"]
    return {
        "comparisons": comparisons,
        "recommended_initial_stream":
            "measured_delay_sum_direct_q14_0_3",
        "mean_snr_gain_over_current_db": (
            measured["mean_snr_db"] - baseline["mean_snr_db"]),
        "minimum_snr_gain_over_current_db": (
            measured["minimum_snr_db"] - baseline["minimum_snr_db"]),
        "reason": (
            "The fixed four-sample lane offset was present in all three "
            "one-metre directions, and the compensated 0/3 pair was the "
            "strongest robust subset in the captured session."
        ),
    }


def write_csv(path: Path, captures: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow([
            "wav", "label", "distance_m", "azimuth_deg", "channel",
            "speech_rms", "noise_rms", "snr_db", "peak",
            "lag_samples", "correlation", "polarity", "miccal_q14",
        ])
        for capture in captures:
            metadata = capture["metadata"]
            for channel in capture["channels"]:
                writer.writerow([
                    capture["wav"],
                    metadata.get("label"),
                    metadata.get("distance_m"),
                    metadata.get(
                        "azimuth_degrees_clockwise_from_front"),
                    channel["channel"],
                    channel["speech_rms"],
                    channel["noise_rms"],
                    channel["snr_db"],
                    channel["peak"],
                    channel.get("lag_samples_from_reference"),
                    channel.get("correlation"),
                    channel.get("suggested_polarity"),
                    channel.get("idme_miccal_q14"),
                ])


def print_summary(captures: list[dict], geometry: dict | None) -> None:
    for capture in captures:
        metadata = capture["metadata"]
        mixes = capture["mix_comparison"]
        print(
            f"{Path(capture['wav']).name}: {metadata.get('distance_m')} m, "
            f"{metadata.get('azimuth_degrees_clockwise_from_front')}°, "
            f"best channel={capture['reference_channel']}"
        )
        print("  ch  speech-rms  noise-rms  snr-dB  lag  corr  polarity")
        for channel in capture["channels"]:
            if channel["channel"] >= ACTIVE_CHANNELS:
                continue
            print(
                f"  {channel['channel']:2d}  "
                f"{channel['speech_rms']:10.1f}  "
                f"{channel['noise_rms']:9.1f}  "
                f"{channel['snr_db']:6.1f}  "
                f"{channel['lag_samples_from_reference']:3d}  "
                f"{channel['correlation']:5.2f}  "
                f"{channel['suggested_polarity']:+d}"
            )
        print(
            "  mix SNR: "
            f"single={mixes['best_single']['snr_db']:.1f} dB, "
            f"current={mixes['current_direct_q14_mix_0_1_3_4']['snr_db']:.1f} dB, "
            "measured-0/3="
            f"{mixes['measured_delay_sum_direct_q14_0_3']['snr_db']:.1f} dB, "
            "aligned="
            f"{mixes['polarity_timing_aligned_direct_q14_mix']['snr_db']:.1f} dB"
        )
    if geometry:
        print(
            f"Fitted geometry relative to channel "
            f"{geometry['reference_channel']}:"
        )
        for position in geometry["positions"]:
            print(
                f"  ch {position['channel']}: "
                f"bias={position['transport_bias_samples']:+.1f}, "
                f"x={position['x_right_mm']:+.1f} mm, "
                f"y={position['y_front_mm']:+.1f} mm, "
                f"fit={position['fit_rms_samples']:.2f} samples"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", nargs="+", type=Path)
    parser.add_argument("--report", type=Path,
                        default=Path("array-analysis.json"))
    parser.add_argument("--csv", type=Path,
                        default=Path("array-analysis.csv"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    captures = [analyse_capture(path) for path in args.wav]
    geometry = fit_geometry(captures)
    report = {
        "schema": 2,
        "captures": captures,
        "session_calibration": session_calibration(captures),
        "session_beamforming": session_beamforming(captures),
        "fitted_geometry": geometry,
    }
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_csv(args.csv, captures)
    print_summary(captures, geometry)
    print(f"Report: {args.report}")
    print(f"CSV: {args.csv}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)

#!/usr/bin/env python3

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import wave

import numpy as np


MODULE_PATH = Path(__file__).with_name("analyze_array.py")
SPEC = importlib.util.spec_from_file_location("analyze_array", MODULE_PATH)
assert SPEC and SPEC.loader
ANALYSE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ANALYSE)


def delay(values: np.ndarray, samples: int) -> np.ndarray:
    output = np.zeros_like(values)
    if samples > 0:
        output[samples:] = values[:-samples]
    elif samples < 0:
        output[:samples] = values[-samples:]
    else:
        output[:] = values
    return output


def write_s24_wav(path: Path, values: np.ndarray) -> None:
    packed_values = (
        np.clip(np.rint(values), -32768, 32767).astype(np.int32) << 8)
    unsigned = packed_values & 0xFFFFFF
    packed = np.empty((*values.shape, 3), dtype=np.uint8)
    packed[:, :, 0] = unsigned & 0xFF
    packed[:, :, 1] = (unsigned >> 8) & 0xFF
    packed[:, :, 2] = (unsigned >> 16) & 0xFF
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(9)
        wav.setsampwidth(3)
        wav.setframerate(16000)
        wav.writeframes(packed.tobytes())


class ArrayAnalysisTest(unittest.TestCase):
    def test_detects_delayed_three_tone_countdown(self) -> None:
        rate = 16000
        samples = np.zeros((rate * 6, 9), dtype=np.float64)
        tone_frames = round(0.12 * rate)
        for onset_seconds in (1.5, 2.5, 3.5):
            start = round(onset_seconds * rate)
            time = np.arange(tone_frames) / rate
            tone = 3000 * np.sin(2 * np.pi * 880 * time)
            samples[start:start + tone_frames, :7] = tone[:, None]
        observed = ANALYSE.detect_countdown(samples, rate)
        self.assertIsNotNone(observed)
        assert observed is not None
        np.testing.assert_allclose(
            observed["tone_onsets_seconds"],
            [1.5, 2.5, 3.5],
            atol=0.02,
        )
        self.assertGreater(
            observed["speech_search_start_seconds"], 3.7)
        self.assertLess(
            observed["speech_search_start_seconds"], 4.0)

    def test_detects_relative_delay_and_inverted_channel(self) -> None:
        generator = np.random.default_rng(7)
        frames = 96000
        source = np.zeros(frames)
        phrase = generator.normal(0, 900, 16000)
        phrase = np.convolve(
            phrase, np.ones(5) / 5.0, mode="same")
        source[32000:48000] = phrase
        channels = np.zeros((frames, 9), dtype=np.float64)
        lags = [0, 3, -4, 1, 5, -2, 2]
        signs = [1, 1, -1, 1, 1, 1, 1]
        for channel in range(7):
            channels[:, channel] = (
                signs[channel] * delay(source, lags[channel]) +
                generator.normal(0, 8, frames)
            )
        channels[:, 7:] = generator.normal(0, 2, (frames, 2))

        with tempfile.TemporaryDirectory() as directory:
            wav_path = Path(directory) / "synthetic.wav"
            write_s24_wav(wav_path, channels)
            wav_path.with_suffix(".json").write_text(json.dumps({
                "wav_file": wav_path.name,
                "label": "synthetic",
                "distance_m": 1.0,
                "azimuth_degrees_clockwise_from_front": 0.0,
                "idme_miccal_q14": [16384] * 7,
            }), encoding="utf-8")
            result = ANALYSE.analyse_capture(wav_path)

        reference = result["reference_channel"]
        reference_lag = lags[reference]
        for channel in range(7):
            metric = result["channels"][channel]
            self.assertLessEqual(
                abs(metric["lag_samples_from_reference"] -
                    (lags[channel] - reference_lag)),
                1,
            )
            self.assertEqual(
                metric["suggested_polarity"],
                signs[channel] * signs[reference],
            )
        self.assertGreater(
            result["channels"][0]["snr_db"],
            result["channels"][7]["snr_db"],
        )

    def test_geometry_separates_transport_bias_from_arrival_time(self) -> None:
        rate_over_sound = 16000.0 / ANALYSE.SPEED_OF_SOUND_M_S
        x_m = 0.021
        y_m = -0.032
        bias = 4.0
        captures = []
        for azimuth in (0.0, 90.0, 270.0):
            angle = np.deg2rad(azimuth)
            lag = (
                bias -
                (x_m * np.sin(angle) + y_m * np.cos(angle)) *
                rate_over_sound
            )
            channels = []
            for channel in range(ANALYSE.ACTIVE_CHANNELS):
                channels.append({
                    "channel": channel,
                    "snr_db": 20.0,
                    "correlation": 0.9,
                    "lag_samples_from_reference":
                        0.0 if channel == 0 else lag,
                })
            captures.append({
                "wav": f"{azimuth}.wav",
                "metadata": {
                    "azimuth_degrees_clockwise_from_front": azimuth,
                },
                "reference_channel": 0,
                "channels": channels,
            })

        geometry = ANALYSE.fit_geometry(captures)
        self.assertIsNotNone(geometry)
        assert geometry is not None
        channel = geometry["positions"][1]
        self.assertAlmostEqual(
            channel["transport_bias_samples"], bias, places=6)
        self.assertAlmostEqual(channel["x_right_mm"], 21.0, places=6)
        self.assertAlmostEqual(channel["y_front_mm"], -32.0, places=6)


if __name__ == "__main__":
    unittest.main()

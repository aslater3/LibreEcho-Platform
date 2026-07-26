#!/usr/bin/env python3

import hashlib
import importlib.util
from pathlib import Path
import struct
import unittest


MODULE_PATH = Path(__file__).with_name("capture_array.py")
SPEC = importlib.util.spec_from_file_location("capture_array", MODULE_PATH)
assert SPEC and SPEC.loader
CAPTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CAPTURE)


class CountdownPcmTest(unittest.TestCase):
    def test_three_tones_are_one_second_apart(self) -> None:
        payload = CAPTURE.countdown_pcm()
        self.assertEqual(
            len(payload),
            CAPTURE.COUNTDOWN_RATE *
            CAPTURE.COUNTDOWN_SECONDS *
            CAPTURE.COUNTDOWN_CHANNELS * 2,
        )
        samples = struct.unpack(f"<{len(payload) // 2}h", payload)
        left = samples[0::2]
        threshold = CAPTURE.COUNTDOWN_AMPLITUDE // 4
        active = [abs(value) >= threshold for value in left]

        for second in range(3):
            start = second * CAPTURE.COUNTDOWN_RATE
            tone = active[start:start + round(
                CAPTURE.COUNTDOWN_RATE *
                CAPTURE.COUNTDOWN_TONE_SECONDS)]
            silence = active[
                start + round(
                    CAPTURE.COUNTDOWN_RATE *
                    CAPTURE.COUNTDOWN_TONE_SECONDS):
                (second + 1) * CAPTURE.COUNTDOWN_RATE
            ]
            self.assertTrue(any(tone))
            self.assertFalse(any(silence))

        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            "64c0889b6cb3b5ed8ad2af8312552d33dd78c576ab371a4f5198561debd143c8",
        )


if __name__ == "__main__":
    unittest.main()

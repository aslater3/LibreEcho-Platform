"""Music visualizer regression corpus (#44/#45/#46), run through the production analyser.

Builds music-trace from the shipped sources, generates the synthetic corpus
(corpus/gen_corpus.py) and checks the traces. Properties that hold are
asserted; known gaps are expected failures so they stay visible and flip to
unexpected successes when fixed:
  - recording level: a -20 dB master halves energy and density.  Playback
    volume no longer reaches the analyser (the engine's unity-gain tap, #44,
    covered by test_music_analysis_tap.sh); a quieter recording still does.
"""
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


def median(rows, key):
    return statistics.median(r[key] for r in rows)


class MusicTraceCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.bin = cls.dir / "music-trace"
        cc = os.environ.get("CC", shutil.which("cc") or "cc")
        subprocess.run([cc, "-O2", "-std=c99", "-Wall", "-Wextra", "-Wpedantic", "-Werror",
                        str(HERE / "audio_visualizer.c"), str(HERE / "music_trace.c"), "-lm", "-o", str(cls.bin)],
                       check=True, timeout=120)
        subprocess.run([sys.executable, str(HERE / "corpus/gen_corpus.py"), str(cls.dir / "corpus")],
                       check=True, timeout=300, capture_output=True)
        cls.annotations = json.loads((cls.dir / "corpus/corpus.json").read_text())
        cls.traces = {}
        for name in cls.annotations:
            out = cls.dir / f"{name}.jsonl"
            subprocess.run([str(cls.bin), str(cls.dir / "corpus" / f"{name}.wav"), str(out)],
                           check=True, timeout=120, capture_output=True)
            rows = [json.loads(line)["args"] for line in out.read_text().splitlines()]
            cls.traces[name] = rows
        cls.settled = {name: rows[len(rows) // 2:] for name, rows in cls.traces.items()}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_trace_is_deterministic_and_production_shaped(self):
        again = self.dir / "again.jsonl"
        subprocess.run([str(self.bin), str(self.dir / "corpus/clicks-120.wav"), str(again)], check=True, timeout=120,
                       capture_output=True)
        self.assertEqual(again.read_bytes(), (self.dir / "clicks-120.jsonl").read_bytes())
        frame = self.traces["clicks-120"][0]
        for key in ("feature_version", "seq", "timestamp_ms", "levels", "beat_phase", "bpm_x100", "events"):
            self.assertIn(key, frame)
        self.assertEqual(frame["feature_version"], 2)

    def test_silence_is_inert(self):
        for row in self.traces["silence"]:
            self.assertEqual((row["energy"], row["events"], row["beat_confidence"], row["onset_low"]), (0, 0, 0, 0))

    def test_beatless_material_reports_no_tempo(self):
        for name in ("pad-beatless", "white-noise", "sweep"):
            with self.subTest(name=name):
                self.assertLess(median(self.settled[name], "beat_confidence"), 32,
                                f"{name} must not be given a confident rhythm")

    def test_click_tracks_have_a_confident_tempo(self):
        for name in ("clicks-090", "clicks-120", "clicks-150"):
            with self.subTest(name=name):
                self.assertGreater(median(self.settled[name], "beat_confidence"), 150)

    def test_120_bpm_tracks_at_the_right_tempo(self):
        bpm = median(self.settled["clicks-120"], "bpm_x100") / 100
        self.assertAlmostEqual(bpm, 120, delta=6)

    def test_90_and_150_bpm_track_at_the_right_metrical_level(self):
        for name, target in (("clicks-090", 90), ("clicks-150", 150)):
            self.assertAlmostEqual(median(self.settled[name], "bpm_x100") / 100, target, delta=target * 0.06)

    def test_volume_keeps_tempo_and_confidence(self):
        loud, quiet = self.settled["clicks-120"], self.settled["clicks-120-minus20db"]
        self.assertEqual(median(loud, "bpm_x100"), median(quiet, "bpm_x100"))
        self.assertAlmostEqual(median(loud, "beat_confidence"), median(quiet, "beat_confidence"), delta=16)

    @unittest.expectedFailure
    def test_volume_keeps_energy_and_density(self):
        loud, quiet = self.settled["clicks-120"], self.settled["clicks-120-minus20db"]
        self.assertAlmostEqual(median(loud, "energy"), median(quiet, "energy"), delta=6)
        self.assertAlmostEqual(median(loud, "density"), median(quiet, "density"), delta=6)

    def test_rejects_unsupported_wav(self):
        bad = self.dir / "bad.wav"
        header = bytearray((self.dir / "corpus/silence.wav").read_bytes()[:44])
        header[24:28] = (44100).to_bytes(4, "little")
        bad.write_bytes(bytes(header) + b"\0" * 400)
        result = subprocess.run([str(self.bin), str(bad), str(self.dir / "bad.jsonl")], capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertIn("48000", result.stderr)


if __name__ == "__main__":
    unittest.main()

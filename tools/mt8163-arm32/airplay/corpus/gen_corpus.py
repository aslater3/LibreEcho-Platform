#!/usr/bin/env python3
"""Generate the synthetic music-visualizer regression corpus (#46).

Integer-only synthesis (a fixed sine table and an LCG noise source), so every
host writes byte-identical WAV files; nothing here is committed as audio.

  gen_corpus.py OUTPUT_DIR      writes *.wav and corpus.json (annotations)
"""
import json
import math
import struct
import sys
from pathlib import Path

RATE = 48000
SINE = [int(round(32767 * math.sin(2 * math.pi * i / 4096))) for i in range(4096)]


def sine(phase_q16: int) -> int:
    return SINE[(phase_q16 >> 4) & 4095]


class Noise:
    def __init__(self, seed: int):
        self.x = seed & 0xFFFFFFFF

    def next(self) -> int:
        self.x = (1664525 * self.x + 1013904223) & 0xFFFFFFFF
        return (self.x >> 16) - 32768


def clip(v: int) -> int:
    return max(-32768, min(32767, v))


def write(path: Path, samples: list[int]) -> None:
    data = struct.pack(f"<{len(samples) * 2}h", *[s for v in samples for s in (v, v)])
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 2, RATE, RATE * 4, 4, 16) + b"data" + struct.pack("<I", len(data))
    path.write_bytes(header + data)


def tone_step(freq_hz: float) -> int:
    """Per-sample phase increment; sine() completes a cycle every 65536."""
    return int(round(freq_hz * 65536 / RATE))


def clicks(bpm: int, seconds: int, freq: float = 80.0) -> list[int]:
    period = RATE * 60 // bpm
    step = tone_step(freq)
    out, phase = [], 0
    for n in range(RATE * seconds):
        k = n % period
        env = max(0, 26000 - (k * 26000) // (RATE // 12))  # ~83 ms decay
        phase = (phase + step) & 0xFFFFFFF
        out.append(clip(env * sine(phase) // 32767))
    return out


def pad(seconds: int) -> list[int]:
    """Beatless sustained chord with a slow swell: no onsets to lock onto."""
    steps = [tone_step(f) for f in (220.0, 277.18, 329.63)]
    phases = [0, 0, 0]
    out = []
    for n in range(RATE * seconds):
        swell = 6000 + (4000 * sine(n * 65536 // (RATE * 6)) // 32767)  # one swell every 6 s
        v = 0
        for i, s in enumerate(steps):
            phases[i] = (phases[i] + s) & 0xFFFFFFF
            v += swell * sine(phases[i]) // 32767
        out.append(clip(v))
    return out


def noise(seconds: int, level: int, seed: int) -> list[int]:
    g = Noise(seed)
    return [g.next() * level // 32768 for _ in range(RATE * seconds)]


def sweep(seconds: int) -> list[int]:
    """Log sweep 60 Hz -> 12 kHz across all twelve bands."""
    out, phase = [], 0
    total = RATE * seconds
    for n in range(total):
        freq = 60.0 * (200.0 ** (n / total))
        phase = (phase + tone_step(freq)) & 0xFFFFFFF
        out.append(clip(20000 * sine(phase) // 32767))
    return out


def scaled(samples: list[int], db: int) -> list[int]:
    factor = round(10 ** (db / 20) * 65536)
    return [clip(s * factor >> 16) for s in samples]


def main() -> int:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    cases = {
        "silence": ([0] * (RATE * 8), {"kind": "silence"}),
        "clicks-090": (clicks(90, 16), {"kind": "beat", "bpm": 90}),
        "clicks-120": (clicks(120, 16), {"kind": "beat", "bpm": 120}),
        "clicks-150": (clicks(150, 16), {"kind": "beat", "bpm": 150}),
        "pad-beatless": (pad(16), {"kind": "beatless"}),
        "white-noise": (noise(8, 12000, 7), {"kind": "noise"}),
        "sweep": (sweep(12), {"kind": "sweep"}),
    }
    cases["clicks-120-minus20db"] = (scaled(cases["clicks-120"][0], -20), {"kind": "beat", "bpm": 120, "gain_db": -20, "copy_of": "clicks-120"})
    annotations = {}
    for name, (samples, meta) in cases.items():
        write(out / f"{name}.wav", samples)
        annotations[name] = meta
    (out / "corpus.json").write_text(json.dumps(annotations, indent=1, sort_keys=True) + "\n")
    print(f"corpus: {len(cases)} files in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Write the latency click track: S16_LE stereo 48 kHz, whole engine periods.

  make_clicks.py OUT [--count 6] [--amplitude 16000]
"""
import argparse
import math
import struct

RATE, PERIOD = 48000, 2048
parser = argparse.ArgumentParser()
parser.add_argument('out')
parser.add_argument('--count', type=int, default=6)
parser.add_argument('--amplitude', type=int, default=16000)
args = parser.parse_args()
frames = [0] * (RATE // 2)
for _ in range(args.count):
    frames += [round(args.amplitude * math.sin(2 * math.pi * 2000 * n / RATE)) if n else args.amplitude
               for n in range(144)]          # 3 ms burst, loud from its first frame
    frames += [0] * (RATE // 2 - 144)
frames += [0] * (-len(frames) % PERIOD + PERIOD)
with open(args.out, 'wb') as f:
    f.write(b''.join(struct.pack('<hh', v, v) for v in frames))

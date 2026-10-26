#!/usr/bin/env python3
"""Per player: electrical click onset (ref7 in an LE_CAP_RAW dump) minus the
time that player's own SDK time filter says the click should play.
Development tool; nothing is compared across device clocks.

Needs sendspind built with LE_SENDSPIND_CLOCK_TRACE (build_sendspind.py
--define LE_SENDSPIND_CLOCK_TRACE; it writes /tmp/le-ss/clock-trace.log) and
the stream_first_play_us line printed by ss_probe_serve.py (--tone 0).
Near zero: the player's local output path is calibrated.  A constant offset
on one player only: that player's engine reports frames as played early or
late (e.g. an uncalibrated speaker-chain delay).

  sync_intent.py FIRST_PLAY_US cap.raw:clock-trace.log [...]
"""
import sys
import numpy as np
dt = np.dtype([('us', '<i8'), ('mic', '<i4'), ('ref', '<i4')])
first = int(sys.argv[1])
res = {}
for pair in sys.argv[2:]:
    raw, trace = pair.split(':')
    a = np.fromfile(raw, dtype=dt)
    ref = np.abs(a['ref']).astype(float)
    thr = ref.max() * 0.3
    idx = np.flatnonzero((ref[1:] > thr) & (ref[:-1] <= thr)) + 1
    on = [idx[0]] + [i for p, i in zip(idx[:-1], idx[1:]) if i - p > 4000]
    tr = []
    for line in open(trace):
        f = dict(x.split('=') for x in line.split())
        tr.append((int(f['mono_us']), int(f['c0']), int(f['c1'])))
    tr = np.array(tr, dtype=float)
    rows = []
    for i in on:
        t = float(a['us'][i])
        j = int(np.argmin(np.abs(tr[:, 0] - t)))
        mono, c0, c1 = tr[j]
        slope = (c1 - c0) / 1e12
        server = (t - c0) / slope
        k = round((server - first) / 1e6)
        intended = c0 + slope * (first + k * 1e6)
        rows.append((k, (t - intended) / 1000.0, (mono - t) / 1000.0))
    print(raw)
    for k, e, age in rows:
        print(f'  click {k:2d} electrical - intended {e:+.3f} ms  (trace sample {age:+.0f} ms away)')
    res[raw] = {k: e for k, e, _ in rows}
    # filter offset evolution over the run (client time of server 0)
    c0s = tr[:, 1]
    print(f'  filter c0 range over run: {(c0s.max() - c0s.min()) / 1000:.3f} ms')

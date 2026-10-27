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

  sync_intent.py FIRST_PLAY_US[,START_US@FRAME0_US...] cap.raw:clock-trace.log [...]

Several timelines (a server restart, or a clear: stream_restart_frame F
play_us P gives P@(P - F/48000 s)) are separated by commas; each click is
mapped to the latest timeline that started before it.  Clicks on any
timeline play at FRAME0 + k seconds.  A summary line per device gives the
median and spread from the first click within 0.5 ms (settled), and the
time from each timeline start to that settled click.
"""
import sys
import numpy as np
dt = np.dtype([('us', '<i8'), ('mic', '<i4'), ('ref', '<i4')])
timelines = []
for part in sys.argv[1].split(','):
    start, _, frame0 = part.partition('@')
    timelines.append((int(start), int(frame0 or start)))
timelines.sort()
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
        live = [tl for tl in timelines if tl[0] <= server + 0.5e6] or timelines[:1]
        tl_index = timelines.index(live[-1])
        frame0 = live[-1][1]
        k = round((server - frame0) / 1e6)
        intended = c0 + slope * (frame0 + k * 1e6)
        rows.append((tl_index, k, (t - intended) / 1000.0, (mono - t) / 1000.0, server))
    print(raw)
    for tl_index, k, e, age, _ in rows:
        print(f'  timeline {tl_index} click {k:4d} electrical - intended {e:+.3f} ms  '
              f'(trace sample {age:+.0f} ms away)')
    for n, (start, _) in enumerate(timelines):
        mine = [r for r in rows if r[0] == n]
        settled = next((i for i, r in enumerate(mine) if abs(r[2]) < 0.5), None)
        if settled is None:
            print(f'  timeline {n}: {len(mine)} clicks, never within 0.5 ms')
            continue
        tail = np.array([r[2] for r in mine[settled:]])
        print(f'  timeline {n}: {len(mine)} clicks, settled {(mine[settled][4] - start) / 1e6:.2f} s '
              f'after start, then median {np.median(tail):+.3f} ms spread {np.ptp(tail):.3f} ms '
              f'max |err| {np.abs(tail).max():.3f} ms (n={len(tail)})')
    res[raw] = {(r[0], r[1]): r[2] for r in rows}
    # filter offset evolution over the run (client time of server 0)
    c0s = tr[:, 1]
    print(f'  filter c0 range over run: {(c0s.max() - c0s.min()) / 1000:.3f} ms')

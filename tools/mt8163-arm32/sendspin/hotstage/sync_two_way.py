#!/usr/bin/env python3
"""Two-way acoustic skew between two grouped players.  Development tool.

Inputs are LE_CAP_RAW dumps from le-latency-cap, captured at the same time on
both devices while ss_probe_serve.py streams short clicks to the group
(--tone 0 --click-ms 0.5) and mutes player A for part of the run
(--script '12:mute,20:unmute' --script-player 0).  B plays throughout.

Each capture has its own clock; nothing is compared across clocks.  Per
device, clicks are indexed on its own electrical reference (ref7, 1 s
spacing fitted on locked clicks, extrapolated where A is muted):

  gA  A's mic, A playing:      A's own click (first arrival)
  xA  A's mic, A muted:        B's click
  gB  B's mic, A muted:        B's own click
  yB  B's mic, A playing:      A's click (must arrive before B's own)

  skew (A minus B, acoustic) = ((yB - gB) - (xA - gA)) / 2
  path between them          = ((yB - gB) + (xA - gA)) / 2

Microphone path delay and room distance cancel; speaker transducer delay is
part of the acoustic skew, as a listener hears it.

  sync_two_way.py A.raw B.raw [--skip N]
"""
import argparse
import numpy as np

RAW = np.dtype([('us', '<i8'), ('mic', '<i4'), ('ref', '<i4')])


def load(path):
    a = np.fromfile(path, dtype=RAW)
    ref = np.abs(a['ref']).astype(float)
    thr = ref.max() * 0.3
    idx = np.flatnonzero((ref[1:] > thr) & (ref[:-1] <= thr)) + 1
    on = [idx[0]] + [i for p, i in zip(idx[:-1], idx[1:]) if i - p > 4000]
    t = a['us'][on].astype(float)
    k = np.round((t - t[0]) / 1e6).astype(int)
    return a, t, k


def fit_grid(t, k, skip):
    keep = k >= skip  # skip the start-up slew
    fit = np.polyfit(k[keep], t[keep], 1)
    return fit, np.abs(t[keep] - np.polyval(fit, k[keep])).max()


def first_arrival(a, t_ref_us, lo_ms=-12.0, hi_ms=12.0, nsig=8.0):
    us = a['us']
    mic = a['mic'].astype(float)
    i0 = np.searchsorted(us, t_ref_us + lo_ms * 1000)
    i1 = np.searchsorted(us, t_ref_us + hi_ms * 1000)
    noise = np.std(mic[max(0, i0 - 1600):max(1, i0 - 160)]) or 1.0
    hit = np.flatnonzero(np.abs(mic[i0:i1]) > nsig * noise)
    return None if not len(hit) else (us[i0 + hit[0]] - t_ref_us) / 1000.0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('a_raw', help='capture from the player muted mid-run')
    p.add_argument('b_raw', help='capture from the player that played throughout')
    p.add_argument('--skip', type=int, default=6, help='start-up clicks to ignore')
    o = p.parse_args()

    a, ta, ka = load(o.a_raw)
    b, tb, kb = load(o.b_raw)
    fa, ra = fit_grid(ta, ka, o.skip)
    fb, rb = fit_grid(tb, kb, o.skip)
    print(f'grid residual A {ra:.0f} us, B {rb:.0f} us')
    played = set(ka.tolist())
    a_rng = range(o.skip, int(ka.max()) + 1)
    gA = [first_arrival(a, np.polyval(fa, k)) for k in a_rng if k in played]
    xA = [first_arrival(a, np.polyval(fa, k)) for k in a_rng if k not in played]
    b_arr = [first_arrival(b, np.polyval(fb, k)) for k in range(o.skip, int(kb.max()) + 1)]
    gA = np.array([v for v in gA if v is not None])
    xA = np.array([v for v in xA if v is not None])
    b_arr = np.array([v for v in b_arr if v is not None])
    if not len(xA) or not len(gA):
        raise SystemExit('A needs both playing and muted clicks after --skip')
    # B's mic: two clusters, A's (earlier) arrival while A plays and B's own
    # while A is muted.
    order = np.sort(b_arr)
    split = int(np.argmax(np.diff(order))) + 1
    yB, gB = order[:split], order[split:]
    if not len(yB) or not len(gB) or gB.min() - yB.max() < 0.3:
        raise SystemExit("B's mic shows no separate early arrival: A does not lead, "
                         'swap the roles (mute B instead)')
    m = {n: float(np.median(v)) for n, v in
         (('gA', gA), ('xA', xA), ('gB', gB), ('yB', yB))}
    for n, v in (('gA', gA), ('xA', xA), ('gB', gB), ('yB', yB)):
        print(f'{n} median {m[n]:+.3f} ms  spread {np.ptp(v):.3f} ms  n={len(v)}')
    skew = ((m['yB'] - m['gB']) - (m['xA'] - m['gA'])) / 2
    path = ((m['yB'] - m['gB']) + (m['xA'] - m['gA'])) / 2
    print(f'skew A-B {skew:+.3f} ms (negative: A plays first)')
    print(f'path {path:.3f} ms (~{path * 34.3:.0f} cm)')


if __name__ == '__main__':
    main()

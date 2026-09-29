#!/usr/bin/env python3
"""Host-only agreement with the compact authored EQ design, not stock parity.

No vendor taps, coefficients or measured spectra are used. The JSON fixture
contains only independently authored shape/Fc/Q/gain inputs, frozen before the
implementation. Response expectations are calculated here in double precision.
"""
import cmath
import ctypes
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
FIT = json.loads((HERE / "speaker_eq26_design.json").read_text())
PEQ = [dict(shape="ls", fc=177.0, q=0.72, gain_db=4.8),
       dict(shape="pk", fc=81.0, q=0.85, gain_db=2.0)]
BRIDGE = r'''
#include <math.h>
#include <string.h>
#include "speaker_dsp.h"
int section_count(void) { return SPEAKER_DSP_SECTIONS; }
void coefficients(int volume, double *out) {
    struct speaker_dsp d;
    int i;
    speaker_dsp_init(&d, volume);
    for (i = 0; i < SPEAKER_DSP_SECTIONS; ++i) {
        out[5*i] = d.sections[i].b0;
        out[5*i+1] = d.sections[i].b1;
        out[5*i+2] = d.sections[i].b2;
        out[5*i+3] = d.sections[i].a1;
        out[5*i+4] = d.sections[i].a2;
    }
}
double scalar(int volume) {
    struct speaker_dsp d;
    double product = 1;
    int i;
    speaker_dsp_init(&d, volume);
    for (i = 0; i < SPEAKER_DSP_SECTIONS; ++i) product *= d.sections[i].b0;
    return speaker_dsp_equalize(&d, 1000) / (1000 * product);
}
double tone(int volume, double f) {
    struct speaker_dsp d;
    double energy = 0, input = 0;
    int n;
    speaker_dsp_init(&d, volume);
    for (n = 0; n < 144000; ++n) {
        int32_t x = (int32_t)lrint(8000 * sin(6.28318530717958647692 * f * n / 48000));
        float y = speaker_dsp_equalize(&d, x);
        if (!isfinite(y)) return NAN;
        if (n >= 96000) { energy += (double)y*y; input += (double)x*x; }
    }
    return 10 * log10(energy / input);
}
/* Every section state, not an integer conversion, must remain finite and decay. */
double impulse_tail(int volume) {
    struct speaker_dsp d;
    double peak = 0;
    int n, i;
    speaker_dsp_init(&d, volume);
    for (n = 0; n < 240000; ++n) {
        float y = speaker_dsp_equalize(&d, n == 0 ? 1000000 : 0);
        if (!isfinite(y)) return INFINITY;
        for (i = 0; i < SPEAKER_DSP_SECTIONS; ++i)
            if (!isfinite(d.sections[i].z1) || !isfinite(d.sections[i].z2)) return INFINITY;
        if (n >= 192000 && fabs(y) > peak) peak = fabs(y);
    }
    return peak;
}
/* Independent old/new streams predict every blended sample, including the
 * commit frame and bypass transitions. No read of candidate scalar fields. */
double transition(int from, int to, int queued) {
    struct speaker_dsp d, old, target, later;
    struct speaker_mbcl dynamics;
    double worst = 0;
    int n, total = SPEAKER_DSP_VOLUME_RAMP_FRAMES;
    speaker_dsp_init(&d, from);
    speaker_dsp_init(&old, from);
    speaker_dsp_init(&target, to);
    speaker_dsp_init(&later, queued);
    speaker_mbcl_init(&dynamics);
    for (n = 0; n < 12000; ++n) {
        int32_t x = (n % 233) * 7 - 811;
        (void)speaker_dsp_process(&d, x);
        (void)speaker_mbcl_process(&dynamics, speaker_dsp_equalize(&old, x));
    }
    speaker_dsp_set_volume(&d, to);
    if (memcmp(&d.mbcl, &dynamics, sizeof(dynamics))) return INFINITY;
    for (n = 0; n < total * 3; ++n) {
        int32_t x = (n % 271) * 11 - 1400;
        float expected, a, b;
        int32_t got, want;
        if (n == total / 3) speaker_dsp_set_volume(&d, queued);
        if (n < total) {
            a = speaker_dsp_equalize(&old, x);
            b = speaker_dsp_equalize(&target, x);
            expected = a + (b-a) * ((float)n / total);
        } else if (n < total * 2) {
            a = speaker_dsp_equalize(&target, x);
            b = speaker_dsp_equalize(&later, x);
            expected = a + (b-a) * ((float)(n-total) / total);
        } else expected = speaker_dsp_equalize(&later, x);
        want = (int32_t)lrintf(speaker_mbcl_process(&dynamics, expected));
        got = speaker_dsp_process(&d, x);
        if (abs(got-want) > worst) worst = abs(got-want);
    }
    return worst;
}
'''


def design(s):
    a = 10 ** (s["gain_db"] / 40)
    w = math.tau * s["fc"] / 48000
    c, alpha = math.cos(w), math.sin(w) / (2 * s["q"])
    if s["shape"] == "pk":
        b = (1 + alpha*a, -2*c, 1-alpha*a)
        den = (1+alpha/a, -2*c, 1-alpha/a)
    else:
        r = 2 * math.sqrt(a) * alpha
        p, m = a+1, a-1
        if s["shape"] == "ls":
            b = (a*(p-m*c+r), 2*a*(m-p*c), a*(p-m*c-r))
            den = (p+m*c+r, -2*(m+p*c), p+m*c-r)
        else:
            b = (a*(p+m*c+r), -2*a*(m+p*c), a*(p+m*c-r))
            den = (p-m*c+r, 2*(m-p*c), p-m*c-r)
    return tuple(v / den[0] for v in (*b, *den[1:]))


def gain(coefficients, f):
    z = cmath.exp(-1j * math.tau * f / 48000)
    return sum(20 * math.log10(abs((b0+b1*z+b2*z*z)/(1+a1*z+a2*z*z)))
               for b0, b1, b2, a1, a2 in coefficients)


class AuthoredEQ26(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="le-eq26-")
        root = Path(cls.tmp.name)
        cls.addClassCleanup(cls.tmp.cleanup)
        src = root / "bridge.c"
        src.write_text('#include <stdlib.h>\n' + BRIDGE)
        so = root / "bridge.so"
        subprocess.run([os.environ.get("CC", "cc"), "-std=c99", "-O2", "-Wall",
                        "-Wextra", "-Werror", "-fPIC", "-shared", "-I", str(HERE),
                        str(src), "-lm", "-o", str(so)], check=True, timeout=60)
        cls.lib = ctypes.CDLL(str(so))
        cls.lib.coefficients.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_double)]
        for name in ("scalar", "impulse_tail"):
            getattr(cls.lib, name).argtypes = [ctypes.c_int]
            getattr(cls.lib, name).restype = ctypes.c_double
        cls.lib.tone.argtypes = [ctypes.c_int, ctypes.c_double]
        cls.lib.tone.restype = ctypes.c_double
        cls.lib.transition.argtypes = [ctypes.c_int] * 3
        cls.lib.transition.restype = ctypes.c_double

    def coefficients(self, v):
        n = self.lib.section_count()
        out = (ctypes.c_double * (5*n))()
        self.lib.coefficients(v, out)
        return [tuple(out[i:i+5]) for i in range(0, 5*n, 5)]

    def test_26_authored_sections_plus_two_unchanged_peq(self):
        self.assertEqual(self.lib.section_count(), 28)
        for v, spec in FIT["anchors"].items():
            with self.subTest(anchor=v):
                self.assertEqual(len(spec["sections"]), 26)
                for actual, expected in zip(self.coefficients(int(v)),
                                            map(design, spec["sections"] + PEQ)):
                    for a, b in zip(actual, expected):
                        self.assertAlmostEqual(a, b, delta=3e-7)

    def test_anchor_scalar_is_applied(self):
        for v, spec in FIT["anchors"].items():
            with self.subTest(anchor=v):
                self.assertAlmostEqual(20 * math.log10(self.lib.scalar(int(v))),
                                       spec["scalar_db"], delta=0.0001)
        self.assertEqual(self.lib.scalar(-1), 1)

    def test_response_agrees_with_authored_double_design(self):
        for v, spec in FIT["anchors"].items():
            actual = self.coefficients(int(v))
            expected = list(map(design, spec["sections"] + PEQ))
            scalar = 20 * math.log10(self.lib.scalar(int(v)))
            for low, high, tolerance in ((40, 16000, 0.05), (15, 23000, 0.15)):
                worst = max(abs(gain(actual, f) + scalar - gain(expected, f) - spec["scalar_db"])
                            for f in (low * (high/low)**(i/1000) for i in range(1001)))
                print(f"anchor {v}: {low}..{high} Hz coefficient-response max error {worst:.6f} dB")
                self.assertLess(worst, tolerance)

    def test_float_runtime_tones_agree_with_authored_design(self):
        for v, spec in FIT["anchors"].items():
            expected = list(map(design, spec["sections"] + PEQ))
            for f in (18, 40, 80, 120, 700, 4000, 9000, 16000, 22000):
                with self.subTest(anchor=v, hz=f):
                    got = self.lib.tone(int(v), f)
                    want = gain(expected, f) + spec["scalar_db"]
                    self.assertTrue(math.isfinite(got))
                    self.assertAlmostEqual(got, want, delta=0.15)

    def test_all_float_poles_stable_and_impulse_states_decay(self):
        for v in FIT["anchors"]:
            with self.subTest(anchor=v):
                radius = 0
                for b0, b1, b2, a1, a2 in self.coefficients(int(v)):
                    self.assertTrue(all(math.isfinite(x) for x in (b0, b1, b2, a1, a2)))
                    root = cmath.sqrt(a1*a1 - 4*a2)
                    radius = max(radius, abs((-a1+root)/2), abs((-a1-root)/2))
                print(f"anchor {v}: maximum float pole radius {radius:.9f}")
                self.assertLess(radius, 1)
                self.assertLess(self.lib.impulse_tail(int(v)), 0.01)

    def test_first_upper_boundary_selects_whole_topology_and_scalar(self):
        for v in range(0, 129):
            anchor = next((a for a in (50, 60, 70, 80, 100) if v <= a), 100)
            self.assertEqual(self.coefficients(v), self.coefficients(anchor))
            self.assertEqual(self.lib.scalar(v), self.lib.scalar(anchor))

    def test_crossfade_and_queued_retarget_share_dynamics_once(self):
        for start, target, queued in ((50, 100, 70), (100, 50, 80),
                                      (-1, 60, 100), (80, -1, 50), (50, 70, -1)):
            with self.subTest(start=start, target=target, queued=queued):
                self.assertLessEqual(self.lib.transition(start, target, queued), 2)


if __name__ == "__main__":
    unittest.main()

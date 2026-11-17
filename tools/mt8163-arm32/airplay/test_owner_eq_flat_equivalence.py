#!/usr/bin/env python3
"""Four-band owner EQ: all flat is the unmodified speaker chains, sample for sample.

The speaker chain headers at BASELINE_REF (default: release/0.14.6 at
011a6fac7, the last commit before the four-band equaliser) and in the work
tree are compiled into the same driver.  A deterministic programme with level
swells and live volume changes (including unknown-volume re-inits on the Dot)
runs through the Radar-Puffin chain (speaker_dsp_process_f) and the Echo Dot
chain (biscuit_dsp_process).  With every band at 0 dB and boost 0 dB, applied
at start and "changed" to the same flat values mid-stream with the live-apply
calls, the new output must be bit-identical to the old.

It also reports host CPU time per 2048-frame period for the flat chain and with
the owner bands on (old: bass+treble shelves; new: the same two shelves, and
all four bands plus boost), so the cost of the extra bands is visible.  The
timings are informational and not asserted.

Usage: test_owner_eq_flat_equivalence.py [BASELINE_REF]
Skips (exit 0, with a message) outside a git checkout with that ref.
"""

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

AIRPLAY = Path(__file__).resolve().parent
# release/0.14.6 before the four-band owner equaliser (bass/treble schema 1).
BASELINE_REF = "011a6fac7da8070cfc6fc7a6c205d3094670beb4"

DRIVER = r'''
#define _POSIX_C_SOURCE 200809L
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "speaker_dsp.h"
#include "biscuit_speaker_dsp.h"

#define PERIOD 2048
#define TWO_PI 6.28318530717958647692

static uint32_t rng = 0x2468aceu;
static double noise(void) { rng = rng * 1664525u + 1013904223u; return (double)(int32_t)rng / 2147483648.0; }

static float programme(long n)
{
    double t = (double)n / 48000.0;
    double level = ((n / 24000) % 4 < 2) ? 0.9 : 0.12;
    return (float)(26000.0 * level * (0.40 * sin(TWO_PI * 55.0 * t) + 0.20 * sin(TWO_PI * 130.0 * t) +
                                      0.15 * sin(TWO_PI * 410.0 * t) + 0.10 * sin(TWO_PI * 2600.0 * t) +
                                      0.05 * sin(TWO_PI * 9000.0 * t) + 0.05 * noise()));
}

static void radar_flat(struct speaker_dsp *d)
{
#ifdef NEW_EQ
    speaker_dsp_set_tone(d, 0.0f, 0.0f, 0.0f, 0.0f);
#else
    speaker_dsp_set_tone(d, 0.0f, 0.0f);
#endif
    speaker_dsp_set_boost(d, 0.0f);
}

static void dot_flat(struct biscuit_dsp *d)
{
#ifdef NEW_EQ
    biscuit_dsp_set_tone(d, 0.0f, 0.0f, 0.0f, 0.0f);
#else
    biscuit_dsp_set_tone(d, 0.0f, 0.0f);
#endif
    biscuit_dsp_set_boost(d, 0.0f);
}

static const int volumes[] = { 50, 70, 100, 60, -1, 80, 30, -1, 90, 100 };

static void equivalence(const char *path)
{
    static struct speaker_dsp radar;
    static struct biscuit_dsp dot;
    FILE *out = fopen(path, "wb");
    long n, total = 48000L * 20;
    int v = 0;

    speaker_dsp_init(&radar, 70);
    radar_flat(&radar);
    biscuit_dsp_init(&dot, 70);
    biscuit_dsp_configure(&dot, 2.5f);
    dot_flat(&dot);
    for (n = 0; n < total; ++n) {
        float x = programme(n), y[2];
        if (n % (2L * 48000L) == 0) {
            speaker_dsp_set_volume(&radar, volumes[v] < 0 ? 40 : volumes[v]);
            biscuit_dsp_set_volume(&dot, volumes[v]);
            v = (v + 1) % (int)(sizeof(volumes) / sizeof(volumes[0]));
        }
#ifdef NEW_EQ
        if (n % 24576 == 12288) { /* the engine's live poll finding flat files */
            speaker_dsp_fade_tone(&radar, 0.0f, 0.0f, 0.0f, 0.0f);
            speaker_dsp_fade_boost(&radar, 0.0f);
            biscuit_dsp_fade_tone(&dot, 0.0f, 0.0f, 0.0f, 0.0f);
            biscuit_dsp_fade_boost(&dot, 0.0f);
        }
#endif
        y[0] = speaker_dsp_process_f(&radar, x);
        y[1] = biscuit_dsp_process(&dot, x);
        fwrite(y, sizeof(y), 1, out);
    }
    fclose(out);
}

static double now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1e6 + (double)ts.tv_nsec / 1e3;
}

/* mode 0 flat, 1 bass+treble shelves (+3/-2), 2 all four bands + boost +3 */
static volatile float sink;

static double bench(int dot, int mode)
{
    static struct speaker_dsp radar;
    static struct biscuit_dsp d;
    const int periods = 600;
    double best = 1e30;
    int rep, p, i;

    for (rep = 0; rep < 5; ++rep) {
        double t0;
        float acc = 0.0f;
        long n = 0;

        speaker_dsp_init(&radar, 70);
        biscuit_dsp_init(&d, 70);
        radar_flat(&radar);
        dot_flat(&d);
#ifdef NEW_EQ
        if (mode == 1) { speaker_dsp_set_tone(&radar, 3.0f, 0.0f, 0.0f, -2.0f);
                         biscuit_dsp_set_tone(&d, 3.0f, 0.0f, 0.0f, -2.0f); }
        if (mode == 2) { speaker_dsp_set_tone(&radar, 3.0f, 2.0f, -2.0f, -2.0f);
                         biscuit_dsp_set_tone(&d, 3.0f, 2.0f, -2.0f, -2.0f);
                         speaker_dsp_set_boost(&radar, 3.0f); biscuit_dsp_set_boost(&d, 3.0f); }
#else
        if (mode >= 1) { speaker_dsp_set_tone(&radar, 3.0f, -2.0f); biscuit_dsp_set_tone(&d, 3.0f, -2.0f); }
        if (mode == 2) return -1.0;
#endif
        t0 = now_us();
        for (p = 0; p < periods; ++p)
            for (i = 0; i < PERIOD; ++i, ++n) {
                float x = (float)(8000.0 * sin((double)n * 0.0117));
                acc += dot ? biscuit_dsp_process(&d, x) : speaker_dsp_process_f(&radar, x);
            }
        sink = acc;
        t0 = (now_us() - t0) / periods;
        if (t0 < best)
            best = t0;
    }
    return best;
}

int main(int argc, char **argv)
{
    int dot, mode;
    if (argc > 1)
        equivalence(argv[1]);
    else
        for (dot = 0; dot <= 1; ++dot)
            for (mode = 0; mode <= 2; ++mode)
                printf("%d %d %.2f\n", dot, mode, bench(dot, mode));
    return 0;
}
'''

MODES = ("all flat", "bass+treble shelves", "all four bands + boost")


def build(work: Path, include: Path, name: str, new: bool) -> Path:
    src = work / f"{name}.c"
    src.write_text(DRIVER)
    exe = work / name
    cmd = ["cc", "-std=c99", "-O2", "-Wall", "-Wextra", "-I", str(include)]
    if new:
        cmd.append("-DNEW_EQ")
    subprocess.run(cmd + [str(src), "-lm", "-o", str(exe)], check=True, timeout=180)
    return exe


def baseline_tree(work: Path) -> Path | None:
    ref = sys.argv[1] if len(sys.argv) > 1 else BASELINE_REF
    git = ["git", "-C", str(AIRPLAY)]
    tree = work / "baseline"
    tree.mkdir()
    archive = subprocess.run(git + ["archive", ref, "--", "."], capture_output=True, timeout=120)
    if archive.returncode:
        return None
    subprocess.run(["tar", "-x", "-C", str(tree)], input=archive.stdout, check=True, timeout=60)
    if "low_mid" in (tree / "speaker_tone.h").read_text():
        print("owner_eq_flat_equivalence: SKIP (baseline already has the four-band equaliser)")
        return None
    return tree


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="owner-eq-flat.") as tmp:
        work = Path(tmp)
        tree = baseline_tree(work)
        if tree is None:
            print("owner_eq_flat_equivalence: SKIP (no git baseline available)")
            return
        old = build(work, tree, "old", False)
        new = build(work, AIRPLAY, "new", True)
        hashes = []
        for exe, tag in ((old, "old"), (new, "new")):
            out = work / f"{tag}.f32"
            subprocess.run([str(exe), str(out)], check=True, timeout=300)
            hashes.append(hashlib.sha256(out.read_bytes()).hexdigest())
            print(f"{tag} chains (Radar, Dot) output sha256 {hashes[-1]} ({out.stat().st_size} bytes)")
        if hashes[0] != hashes[1]:
            raise SystemExit("owner_eq_flat_equivalence: flat four-band EQ DIFFERS from the unmodified chains")

        timing = {}
        for exe, tag in ((old, "old"), (new, "new")):
            run = subprocess.run([str(exe)], capture_output=True, text=True, check=True, timeout=600)
            for line in run.stdout.split("\n"):
                if line:
                    dot, mode, us = line.split()
                    timing[(tag, int(dot), int(mode))] = float(us)
        print("host CPU per 2048-frame period (best of 5 x 600 periods, -O2; informational):")
        for dot, chain in ((0, "Radar-Puffin"), (1, "Echo Dot")):
            for mode, label in enumerate(MODES):
                old_us = timing[("old", dot, mode)]
                old_txt = "n/a" if old_us < 0 else f"{old_us:7.1f} us"
                print(f"  {chain:12s} {label:24s} before {old_txt:>10s}  after {timing[('new', dot, mode)]:7.1f} us")
    print("owner_eq_flat_equivalence: all-flat four-band EQ is the unmodified Radar and Dot chains PASS")


if __name__ == "__main__":
    main()

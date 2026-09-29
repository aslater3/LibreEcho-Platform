# Experimental authored EQ26

This branch is an experiment, not a production promotion or stock-parity claim.
It replaces the normalized five-section loudness approximation with a frozen,
independently authored 26-section design per volume boundary, followed by the
existing two parametric-EQ sections. No fit refinement was performed.

## Inputs and processing

`speaker_eq26_design.json` contains only compact shape/Fc/Q/gain parameters and
per-anchor scalar dB values. It deliberately excludes stock taps, coefficient
arrays, spectra and private measurement results. The original input file's
SHA-256 is `5e87e4869f138cad20da48f542208b2c84ccac1ea3f2ed2cd351b3a4c6b4ead7`.
The header contains the same authored parameters; the JSON is a host-test
fixture, not a runtime dependency.

The first upper boundary remains 50, 60, 70, 80 or 100. Each selected anchor has
its own complete Fc/Q/gain topology and scalar (respectively +7.680, +8.508,
-11.068, -2.734 and -4.987 dB). The scalar is applied after the linear cascade,
before the shared MBCL. Unknown volume retains the existing EQ bypass behavior.

Both sides of the existing 4800-frame transition carry their own scalar and
filter history; the scalar commits with the target state before any queued
transition starts. MBCL runs once after the blended EQ output. Logical-volume
mapping, codec reference, master attenuation, MBCL, mono duplication, output
trim and final output protection are unchanged.

Coefficient design uses double arithmetic, then stores normalized coefficients
and processes samples in float. In host comparison, the previous float designer
produced up to 0.064362 dB coefficient-response error at anchor 60 over 15–23000 Hz;
the double designer reduced that anchor's maximum to 0.002313 dB. The final
maximum across anchors is 0.012600 dB over 40–16000 Hz and 0.022668 dB over
15–23000 Hz, relative to the authored double design, **not the stock response**.

## Host checks

Run from the repository root:

```sh
bash tools/mt8163-arm32/airplay/test_speaker_dsp.sh
bash tools/mt8163-arm32/airplay/test_speaker_mbcl.sh
for test in test_audio_engine_contract test_audio_period_buffer \
  test_airplay_volume_contract test_airplay_session_dsp \
  test_airplay_generation_fence test_airplay_hook_identity \
  test_shairport_hook_patch; do
  python3 "tools/mt8163-arm32/airplay/${test}.py" || exit
done
```

The DSP script includes the new seven-test EQ26 host suite. It verifies the
28 sections against an independent double designer with float-rounded inputs
and outputs; absolute scalar; coefficient-response error; real float-runtime
tones including low frequencies; all poles inside the unit circle; finite,
decaying float impulse state; all boundary selections; and sample-by-sample
crossfade/queued retargeting including unknown-volume transitions and shared
MBCL history. Temporary mutation checks rejected missing target scalar, missing
committed scalar, and the old float-only coefficient designer.

TDD checkpoints recorded failures for missing section count/scalars/response
before implementation. One initial coefficient assertion required less than
half a float ULP at a large coefficient; it was corrected to require exact
float-rounded agreement instead. Response thresholds were unchanged.

The old normalized-curve treble/mid-band assumptions do not describe this
absolute authored design; those checks now use independent authored anchor
expectations. The headroom stimulus increased from 4000 to 8000 PCM units to
retain the same 45000–80000 pre-MBCL peak assertion despite lower 80 Hz boost.
Output-rail, MBCL and limiter thresholds were not relaxed.

## Remaining gates

Host agreement and stability do not prove stock parity, listening quality,
physical speaker distortion, or real-time ARM32 performance. EQ work increases
from seven to 28 sections, and two cascades run during transitions. Target CPU
cost, target numerical behavior, supervised listening and packaging/deployment
remain separate gates. This change does not deploy or push anything.

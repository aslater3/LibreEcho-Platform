# MT8163 microphone-array calibration

This workflow records the complete Radar microphone transport before choosing
a channel map or beamformer:

```text
9 × S24_3LE at 16 kHz
  ├── raw lanes 0..6: microphone candidates
  └── raw lanes 7..8: currently treated as inactive transport lanes
```

The original LibreEcho wake path mapped raw lanes 0..6 directly to logical
microphones and averaged logical channels `[0,1,3,4]`. That four-channel subset
was a provisional hypothesis inherited from earlier stock-pipeline notes. No
retained stock configuration proves that it is the stock classifier input or
physical array subset.

The 26 July 2026 session measured front, right and left takes at one metre.
All seven candidate lanes had positive polarity. Lane 6 was consistently the
noisiest, and lane 3 had a fixed four-sample transport offset from lane 0 in
all three directions. The initial production stream therefore uses a causal
two-lane delay-and-sum:

```text
IDME direct-Q14 lane 0 → delay 4 samples ┐
                                         ├→ mean → 80 Hz HPF
IDME direct-Q14 lane 3 → delay 0 samples ┘
```

This is a measured first-stage beamformer, not a claim that the complete array
geometry has been solved. Keep all nine raw lanes available for later diagonal
or rear validation and wider steerable-beam work.

The energy VAD uses a 16 RMS safety floor by default, then continuously tracks
the observed non-speech noise energy. A steady fan therefore raises the live
threshold automatically. The floor can be overridden persistently at boot by
putting one integer from 1 through 1024 in:

```text
/data/libreecho/config/vad-floor-rms
```

The daemon exposes both `vad_floor_rms` and the learned `vad_noise_energy` in
its status response. Do not set the floor to the measured background level:
it is only a lower bound for the adaptive estimator.

In the 2 m front test, the old 96 RMS floor blocked a model score of 0.999253.
With the measured 16 RMS floor, all three spoken `Alexa` repetitions emitted
wake events.

## One calibration point

Do not run a TTS prompt containing the wake phrase and do not use the LED wake
pattern as a cue. TTS can self-trigger the classifier, and the pulsing LED has
been observed to inject severe interference into raw microphone recordings.

The bounded countdown mode starts raw capture first and then writes a
three-second cue through audiod's system bus:

```text
0.00 s: 120 ms tone
1.00 s: 120 ms tone
2.00 s: 120 ms tone
3.00 s: speech-analysis window begins
```

After the third tone, say `Alexa` three times at a normal pace. A 15-second
capture leaves approximately 12 seconds for speech:

```sh
cd /home/andy/workspace/mt8163-arm32-wifi-candidate/LibreEcho-Kernel

python3 tools/mt8163-arm32/wakeword/capture_array.py \
  --label front-1m \
  --distance-m 1 \
  --azimuth-deg 0 \
  --duration 15 \
  --lead-seconds 0 \
  --countdown-tones \
  --output-dir \
    /home/andy/workspace/mt8163-arm32-wifi-candidate/audio-calibration/session-20260726
```

`0°` is the operator's original front test position; `90°` is the operator's
right from that position and `270°` is the left. The enclosure was dismantled,
so the Amazon logo was not a usable reference. Run one point at a time so the
operator can confirm position before capture starts.

The host stages only a deterministic 48 kHz stereo S16 countdown cue. On the
device, the root controller:

1. stops `waked` and waits for its microphone stream to close;
2. starts bounded nine-channel `tinycap`;
3. writes the countdown through `/run/libreecho-audio/system.pcm`;
4. waits for the fixed-duration capture;
5. restores `waked` under an EXIT trap;
6. lets the host pull the WAV; and
7. deletes all temporary device files.

It does not reboot, flash, modify IDME, or retain audio on the device.

## Analysis

Analyse all accepted points together:

```sh
python3 tools/mt8163-arm32/wakeword/analyze_array.py \
  /home/andy/workspace/mt8163-arm32-wifi-candidate/audio-calibration/session-20260726/*.wav \
  --report /home/andy/workspace/mt8163-arm32-wifi-candidate/audio-calibration/session-20260726/analysis.json \
  --csv /home/andy/workspace/mt8163-arm32-wifi-candidate/audio-calibration/session-20260726/analysis.csv
```

The report includes per-channel speech/noise RMS, SNR, clipping, relative
arrival delay, correlation, suggested polarity, direct/off/inverse IDME
calibration comparisons, and these initial mono candidates:

- best single microphone;
- current direct-Q14 `[0,1,3,4]` average; and
- measured direct-Q14 `[0,3]` delay-and-sum; and
- timing/polarity-aligned direct-Q14 mix.

With at least three distinct known azimuths, the analyser separates per-lane
fixed transport bias from direction-dependent arrival time and fits relative
two-dimensional coordinates. Three directions determine that model exactly
but cannot validate it; a rear or diagonal take is still required before using
the inferred coordinates for a wider steerable beamformer.

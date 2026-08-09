# LibreEcho TTS payload — third-party notices

This payload is a collective work. Each component remains under its own license;
LibreEcho does not relicense third-party code, data, or model weights.

## Sherpa-ONNX and ONNX Runtime

- **sherpa-onnx** — Apache License 2.0
  - https://github.com/k2-fsa/sherpa-onnx
- **ONNX Runtime** — MIT License
  - https://github.com/microsoft/onnxruntime

The exact source commits and build inputs are recorded in the release provenance
and SPDX SBOM. Their license and notice files accompany the public source offer.

## eSpeak NG

The phonemizer data under each voice's `espeak-ng-data/` directory comes from
eSpeak NG and is distributed under GPL-3.0-or-later:

- https://github.com/espeak-ng/espeak-ng

The exact source archive and build configuration accompany the public source
offer.

## Northern English male voice

`models/northern-male/model.onnx` is derived without graph changes from:

- Piper voice: `en_GB-northern_english_male-medium`
- Pinned repository revision: `ea046e8458f6acd997706d6e6066a022b42f6fb1`
- Upstream model SHA-256:
  `57a219ae8e638873db7d18893304be5069c42868f392bb95c3ff17f0690d0689`
- LibreEcho metadata-only model SHA-256:
  `786158f6507d49981889ece1803d8296adfcd34da847eb7e4ef69688ee148119`
- Dataset: OpenSLR SLR83, Northern English male
- License: Creative Commons Attribution-ShareAlike 4.0 International
  (`CC-BY-SA-4.0`)
- Source/model card:
  https://huggingface.co/rhasspy/piper-voices/tree/ea046e8458f6acd997706d6e6066a022b42f6fb1/en/en_GB/northern_english_male/medium

LibreEcho's only modification is the addition of seven descriptive ONNX
metadata properties. The graph and tensor data are byte-for-byte equivalent
when metadata is removed.

## Southern English female voice

`models/southern-female/model.onnx` is derived without graph changes from:

- Piper voice: `en_GB-southern_english_female-low`
- Pinned repository revision: `ea046e8458f6acd997706d6e6066a022b42f6fb1`
- Upstream model SHA-256:
  `f2f37aed1b3a093476f719d1379ba0c0b1b1cf6f1ef99288e2ebf502971a07c3`
- LibreEcho metadata-only model SHA-256:
  `cf7f487689da2ec115cb5e9b5fb5ff4450f24e0c45565e0b72dd1eb4ed4caf65`
- Dataset: OpenSLR SLR83, Southern English female
- License: Creative Commons Attribution-ShareAlike 4.0 International
  (`CC-BY-SA-4.0`)
- Source/model card:
  https://huggingface.co/rhasspy/piper-voices/tree/ea046e8458f6acd997706d6e6066a022b42f6fb1/en/en_GB/southern_english_female/low

LibreEcho's only modification is the addition of seven descriptive ONNX
metadata properties. The graph and tensor data are byte-for-byte equivalent
when metadata is removed.

## Compiler runtime

The statically linked runtime may contain GCC runtime-library code under
GPL-3.0-or-later WITH GCC-exception-3.1 and GNU C Library code under
LGPL-2.1-or-later. Exact toolchain versions, corresponding source, and the
LibreEcho source/build instructions needed for relinking accompany the public
source offer.

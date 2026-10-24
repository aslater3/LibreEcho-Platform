# Sendspin development hot-stage

Development-only tools for running a candidate engine and `sendspind` on a
device without an image build. Everything lives in RAM and is undone by
`sendspin-hotstage.sh unstage`, `le-device restore` or any reboot. Nothing here
is packaged or shipped.

| File | Purpose |
|---|---|
| `build_engine.sh` | Static ARM build of `libreecho-audio-engine` (rootless GCC 15). `ENGINE_CFLAGS=-DLE_ENGINE_LATENCY_TRACE` adds the latency trace; `LATENCY_TOOLS=1` also builds `le-latency-cap`. |
| `build_sendspind.py` | Stages the pinned SDK from the verified archives and cross-builds the UI repo's `sendspind` statically. |
| `sendspin-hotstage.sh` | Device side: bind-mounts `/tmp/le-ss/libreecho-audio-engine` over the airplay2 feature engine, restarts it, starts `sendspind --run --dev-allow-unpaired`. `unstage` reverses it. |
| `le_latency_cap.c`, `make_clicks.py`, `latency-measure.sh` | Output latency measurement: the trace engine logs when each click passes the ALSA hardware pointer; `le-latency-cap` stamps the mic array (and its electrical reference channels 7/8) on the same CLOCK_MONOTONIC. |

Typical flow (device lease held, `LE_OWNER` set):

```sh
./build_engine.sh "$TINYALSA_ARCHIVE" "$OUT"
./build_sendspind.py --ui "$UI" --archives "$ARCHIVES" --out "$OUT"
for f in "$OUT"/libreecho-audio-engine "$OUT"/sendspind sendspin-hotstage.sh; do
    le-device stage dot "$f" /tmp/le-ss/"$(basename "$f")"
done
# then on the device (le-device sh --script): sh /tmp/le-ss/sendspin-hotstage.sh stage
```

`--dev-allow-unpaired` lets any host on the LAN stream to the device while it
runs. Unstage when finished.

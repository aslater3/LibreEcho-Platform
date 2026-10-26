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
| `ss_probe_serve.py` | Host-side test server (pinned aiosendspin with the `server` extra): connects to players directly, trusts them, groups them and streams a tone and/or a click every second. It ends the stream only after the last chunk's play time, because `stream/end` drops audio the player has not played yet. |
| `le_latency_cap.c`, `make_clicks.py`, `latency-measure.sh` | Output latency measurement: the trace engine logs when each click passes the ALSA hardware pointer; `le-latency-cap` stamps the mic array (and its electrical reference channels 7/8) on the same CLOCK_MONOTONIC. |
| `sync_two_way.py` | Grouped sync: run `le-latency-cap` with `LE_CAP_RAW=FILE` on two players at once while `ss_probe_serve.py --tone 0 --click-ms 0.5 --script '12:mute,20:unmute' --script-player 0` mutes the leading player for part of the run. Each mic hears its own click and the other speaker's; the two-way difference cancels room distance and microphone delay and leaves the acoustic skew. |

Typical flow (device lease held, `LE_OWNER` set):

```sh
./build_engine.sh "$TINYALSA_ARCHIVE" "$OUT"
./build_sendspind.py --ui "$UI" --archives "$ARCHIVES" --out "$OUT"
for f in "$OUT"/libreecho-audio-engine "$OUT"/sendspind sendspin-hotstage.sh; do
    le-device stage dot "$f" /tmp/le-ss/"$(basename "$f")"
done
# then on the device (le-device sh --script): sh /tmp/le-ss/sendspin-hotstage.sh stage
```

`ENGINE_CFLAGS=-DLE_ENGINE_SENDSPIN_TRACE` logs every Sendspin period, the
generation open and each output-start step to
`/run/libreecho-audio/sendspin-trace.log`; `le_latency_cap` also prints a
100 ms peak-level timeline for mic 0 and reference 7.

`--dev-allow-unpaired` lets any host on the LAN stream to the device while it
runs. Unstage when finished.

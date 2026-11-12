#!/usr/bin/env python3
"""AirPlay session end/replacement and engine start path (issue #356, RC report).

The real run_engine() loop runs against a fake TinyALSA PCM (a ring that drains
at 48 kHz and logs every call) and a fake mixer whose calls can be slow like
the real codec's, with the real bridge hooks (--start/--stop) driving the
session marker, reset and volume handshake.  Proves:

* Session end (Stop) and replacement (Stop -> new song, Pause -> resume) drop
  what the ALSA ring still holds: a pcm_stop() with a non-empty ring is seen,
  nothing of the ended session is written afterwards, and the first period of
  the new session lands in an empty ring.
* Generation fencing is intact: once the new session's audio plays, nothing
  cuts it (no further drop).
* A flush that leaves the marker alone (skip, seek) is not a session boundary
  and does not drop the ring.
* A ring shared with another bus (TTS on the system bus) is never dropped.
* The engine keeps publishing its output-delay snapshot through the slow start
  path (arm, open, prepare, verify, power, first write), with a truthful
  "starting" figure (not draining, first period queued, nothing in ALSA), so
  delay() never sees a page older than LE_AD_STALE_NS.  Those snapshots carry the time the PCM is expected to drain and every exit
  from the start path clears it.
* Source contract: the drop sits between read_sources() and the period write
  and is cleared when a fresh PCM is opened.
"""
import os
import re
import signal
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import test_airplay_generation_fence as fence
from test_airplay_session_dsp import HERE

CC = os.environ.get("CC", "cc")
PERIOD_FRAMES = 2048
STALE_NS = 250_000_000

HARNESS = r'''
#define _GNU_SOURCE
#define main engine_main
#include "audio_engine.c"
#undef main
#include <stdarg.h>

struct pcm { int ready; unsigned int ring; uint64_t at_ns; };
struct mixer { int unused; };
struct mixer_ctl { int unused; };
static struct mixer g_mixer;
static struct mixer_ctl g_ctl;
static FILE *g_log;

static uint64_t ms_now(void) { return le_ad_now_ns() / 1000000ULL; }
static void L(const char *format, ...)
{
	va_list ap;
	fprintf(g_log, "%llu ", (unsigned long long)ms_now());
	va_start(ap, format);
	vfprintf(g_log, format, ap);
	va_end(ap);
	fputc('\n', g_log);
	fflush(g_log);
}
static void nap(const char *name)
{
	const char *value = getenv(name);
	if (value && atoi(value) > 0)
		usleep((useconds_t)atoi(value) * 1000u);
}
/* The ring drains at the DAC rate. */
static unsigned int settle(struct pcm *pcm)
{
	uint64_t now = le_ad_now_ns(), frames = (now - pcm->at_ns) * 48ULL / 1000000ULL;
	pcm->ring = frames >= pcm->ring ? 0u : pcm->ring - (unsigned int)frames;
	pcm->at_ns = now;
	return pcm->ring;
}
struct pcm *pcm_open(unsigned int card, unsigned int device, unsigned int flags,
		     const struct pcm_config *config)
{
	struct pcm *pcm = calloc(1, sizeof(*pcm));
	(void)card; (void)device; (void)flags;
	nap("LE_FAKE_OPEN_MS");
	pcm->ready = config->format == PCM_FORMAT_S16_LE; /* 24-bit refused */
	pcm->at_ns = le_ad_now_ns();
	if (pcm->ready)
		L("open");
	return pcm;
}
int pcm_is_ready(struct pcm *pcm) { return pcm->ready; }
const char *pcm_get_error(struct pcm *pcm) { (void)pcm; return "fake"; }
void pcm_close(struct pcm *pcm)
{
	if (pcm->ready)
		L("close ring=%u", settle(pcm));
	free(pcm);
}
int pcm_prepare(struct pcm *pcm) { (void)pcm; L("prepare"); return 0; }
int pcm_stop(struct pcm *pcm)
{
	L("stop ring=%u", settle(pcm));
	pcm->ring = 0;
	return 0;
}
int pcm_writei(struct pcm *pcm, const void *data, unsigned int frames)
{
	unsigned int before = settle(pcm);
	(void)data;
	while (settle(pcm) + frames > 4096u)
		usleep(1000);
	L("write pre=%u", before);
	pcm->ring += frames;
	return (int)frames;
}
unsigned int pcm_get_buffer_size(const struct pcm *pcm) { (void)pcm; return 4096u; }
int pcm_get_htimestamp(struct pcm *pcm, unsigned int *avail, struct timespec *ts)
{
	*avail = 4096u - settle(pcm);
	clock_gettime(CLOCK_MONOTONIC, ts);
	return 0;
}
struct mixer *mixer_open(unsigned int card) { (void)card; nap("LE_FAKE_MIXER_MS"); return &g_mixer; }
void mixer_close(struct mixer *m) { (void)m; }
struct mixer_ctl *mixer_get_ctl_by_name(struct mixer *m, const char *n) { (void)m; (void)n; return &g_ctl; }
int mixer_ctl_set_enum_by_string(struct mixer_ctl *c, const char *v) { (void)c; (void)v; return 0; }
unsigned int mixer_ctl_get_num_values(struct mixer_ctl *c) { (void)c; return 2; }
int mixer_ctl_set_value(struct mixer_ctl *c, unsigned int i, int v) { (void)c; (void)i; (void)v; return 0; }
int mixer_ctl_get_value(struct mixer_ctl *c, unsigned int i) { (void)c; (void)i; return 127; }

int main(int argc, char **argv)
{
	struct sigaction action;
	if (argc != 3) return 2;
	g_log = fopen(argv[2], "w");
	if (!g_log) return 2;
	memset(&action, 0, sizeof(action));
	action.sa_handler = on_signal;
	sigemptyset(&action.sa_mask);
	sigaction(SIGTERM, &action, NULL);
	signal(SIGPIPE, SIG_IGN);
	(void)run_engine(argv[1], 0u, 23u);
	return 0;
}
'''


def wait_for(predicate, timeout=5.0, message="timed out"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    raise AssertionError(message)


class Rig:
    def __init__(self, directory, name, mixer_ms=0, open_ms=0):
        self.root = directory / name
        self.root.mkdir()
        fence.compile_fixture(self.root)
        (self.root / "harness.c").write_text(HARNESS)
        subprocess.run([CC, "-std=c99", "-Wall", "-Wextra", "-Werror", "-ffunction-sections",
                        "-fdata-sections", "-I", str(self.root), "-I", str(HERE),
                        str(HERE / "audio_sink.c"), str(HERE / "audio_timing.c"),
                        str(HERE / "aec_reference.c"), str(HERE / "playback_status.c"),
                        str(HERE / "audio_visualizer.c"),
                        str(self.root / "harness.c"),
                        "-Wl,--gc-sections", "-lm", "-o", str(self.root / "harness")],
                       check=True, timeout=90)
        self.log_path = self.root / "pcm.log"
        env = {**os.environ, "LE_FAKE_MIXER_MS": str(mixer_ms), "LE_FAKE_OPEN_MS": str(open_ms)}
        self.engine = subprocess.Popen([str(self.root / "harness"), str(self.root),
                                        str(self.log_path)], env=env,
                                       stderr=open(self.root / "engine.err", "w"))
        wait_for(lambda: (self.root / "airplay-media.pcm").exists()
                 and (self.root / "airplay.engine-delay").exists(), 5, "engine did not start")
        self.fifo = os.open(self.root / "airplay-media.pcm", os.O_WRONLY | os.O_NONBLOCK)
        self.system = os.open(self.root / "system.pcm", os.O_WRONLY | os.O_NONBLOCK)
        self.feeding = None

    def events(self):
        out = []
        for line in self.log_path.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                out.append((int(parts[0]), parts[1], dict(p.split("=") for p in parts[2:])))
        return out

    def names(self):
        return [name for _, name, _ in self.events()]

    def start_session(self, token):
        assert fence.hook(self.root, "--start", token) == 0
        fence.admit(self.root, token)

    def feed(self, fd, value=3000, cadence=0.035):
        """A producer writing one period every `cadence` s, faster than real time."""
        stop = threading.Event()
        period = struct.pack("<h", value) * (PERIOD_FRAMES * 2)

        def run():
            # Like Shairport: a deep backlog first, then slightly faster than
            # real time (a dribbling source would let the engine close the PCM).
            for _ in range(6):
                try:
                    os.write(fd, period)
                except OSError:
                    break
            while not stop.is_set():
                try:
                    os.write(fd, period)
                except BlockingIOError:
                    pass
                except OSError:
                    return
                time.sleep(cadence)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return stop, thread

    def close(self):
        for fd in (self.fifo, self.system):
            try:
                os.close(fd)
            except OSError:
                pass
        self.engine.send_signal(signal.SIGTERM)
        try:
            self.engine.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.engine.kill()


def stop_feed(feed):
    feed[0].set()
    feed[1].join(timeout=2)


def writes(rig):
    return [i for i, n in enumerate(rig.names()) if n == "write"]


def scenario_session_end_drops_ring(directory):
    rig = Rig(directory, "end")
    try:
        rig.start_session("a" * 32)
        feed = rig.feed(rig.fifo)
        wait_for(lambda: len(writes(rig)) >= 6, 5, "AirPlay never played")
        before = rig.names().count("stop")
        assert before == 0, "no boundary yet, nothing may be dropped"
        stop_feed(feed)
        assert fence.hook(rig.root, "--stop", "a" * 32) == 0  # Stop: marker gone
        wait_for(lambda: "close" in rig.names()[len(rig.names()) - 3:] or rig.names()[-1] == "close",
                 3, "engine never closed the PCM after the session ended")
        events = rig.events()
        names = [n for _, n, _ in events]
        stops = [i for i, n in enumerate(names) if n == "stop"]
        assert len(stops) == 1, names[-8:]
        assert int(events[stops[0]][2]["ring"]) > 0, "the ring was already empty: nothing proved"
        assert not any(n == "write" for n in names[stops[0]:]), "audio of the ended session played on"
        assert names.index("close", stops[0]) > stops[0]
    finally:
        rig.close()


def scenario_replacement_drops_ring_and_fences(directory):
    rig = Rig(directory, "replace")
    try:
        rig.start_session("a" * 32)
        feed = rig.feed(rig.fifo, 3000)
        wait_for(lambda: len(writes(rig)) >= 6, 5, "AirPlay never played")
        stop_feed(feed)
        # Stop -> new song / Pause -> resume: the start hook ends A and begins B.
        marker = len(rig.events())
        rig.start_session("b" * 32)
        feed = rig.feed(rig.fifo, -3000)
        wait_for(lambda: [n for _, n, _ in rig.events()[marker:]].count("write") >= 6, 8,
                 "the new session never played")
        events = rig.events()[marker:]
        names = [n for _, n, _ in events]
        first_stop = names.index("stop")
        assert int(events[first_stop][2]["ring"]) > 0, events[: first_stop + 1]
        first_b_write = next(i for i, n in enumerate(names) if n == "write" and i > first_stop)
        assert int(events[first_b_write][2]["pre"]) == 0, \
            "the new session's first period queued behind old audio in the ring"
        # Fence: B's audio is never cut once it plays.
        stops_at_first_b = names[:first_b_write].count("stop")
        time.sleep(0.4)
        names_now = [n for _, n, _ in rig.events()[marker:]]
        assert names_now.count("stop") == stops_at_first_b, "a drop cut the new session"
        assert names_now.count("write") >= 8
        stop_feed(feed)
    finally:
        rig.close()


def scenario_flush_without_boundary_keeps_ring(directory):
    rig = Rig(directory, "flush")
    try:
        rig.start_session("a" * 32)
        feed = rig.feed(rig.fifo)
        wait_for(lambda: len(writes(rig)) >= 6, 5, "AirPlay never played")
        # What the bridge's flush does: a reset request with the marker untouched.
        token = "c" * 32 + "\n"
        (rig.root / "airplay.reset").write_text(token)
        wait_for(lambda: (rig.root / "airplay.reset-ack").exists()
                 and (rig.root / "airplay.reset-ack").read_text() == token, 3, "reset not acked")
        mark = len(writes(rig))
        wait_for(lambda: len(writes(rig)) >= mark + 4, 5, "AirPlay stopped after the flush")
        assert "stop" not in rig.names(), "a flush with an unchanged marker must not drop the ring"
        stop_feed(feed)
    finally:
        rig.close()


def scenario_shared_ring_is_not_dropped(directory):
    rig = Rig(directory, "shared")
    try:
        rig.start_session("a" * 32)
        air = rig.feed(rig.fifo)
        tts = rig.feed(rig.system, 2000)
        wait_for(lambda: len(writes(rig)) >= 6, 5, "nothing played")
        stop_feed(air)
        assert fence.hook(rig.root, "--stop", "a" * 32) == 0
        mark = len(writes(rig))
        wait_for(lambda: len(writes(rig)) >= mark + 5, 5, "the system bus stopped playing")
        assert "stop" not in rig.names(), "TTS in the shared ring was cut by an AirPlay session end"
        stop_feed(tts)
    finally:
        rig.close()


def read_page(path):
    raw = path.read_bytes()
    magic, seq, epoch, mono, read_bytes, queue, alsa, playing, valid = \
        struct.unpack("<IIQQQIIII", raw[:48])
    due = struct.unpack("<Q", raw[48:56])[0]
    return seq, mono, queue, alsa, playing, valid, due


def scenario_start_path_keeps_the_page_fresh(directory):
    # Real hardware spends a long time in mixer and PCM calls; model it: four
    # mixer sessions of 90 ms and two PCM opens of 60 ms add up to well over
    # LE_AD_STALE_NS, so a single publish before and after cannot be fresh.
    rig = Rig(directory, "start", mixer_ms=90, open_ms=60)
    page = rig.root / "airplay.engine-delay"
    try:
        samples = []
        sampling = threading.Event()
        done = threading.Event()

        def sample():
            last = None
            while not done.is_set():
                if sampling.is_set():
                    try:
                        seq, mono, queue, alsa, playing, valid, due = read_page(page)
                    except (OSError, struct.error):
                        continue
                    if seq != last and seq % 2 == 0:
                        samples.append((time.monotonic(), queue, alsa, playing, mono, due))
                        last = seq
                time.sleep(0.001)
        thread = threading.Thread(target=sample, daemon=True)
        thread.start()
        try:
            rig.start_session("a" * 32)
            sampling.set()
            feed = rig.feed(rig.fifo)
            wait_for(lambda: len(writes(rig)) >= 3, 8, "the session never started")
            time.sleep(0.1)
            stop_feed(feed)
        finally:
            done.set()
            thread.join(timeout=2)
        assert len(samples) >= 4, samples
        gaps = [b[0] - a[0] for a, b in zip(samples, samples[1:])]
        worst = max(gaps)
        assert worst < STALE_NS / 1e9 * 0.8, \
            f"engine published nothing for {worst * 1000:.0f} ms during start (limit {STALE_NS // 10**6} ms)"
        starting = [s for s in samples if s[1] >= PERIOD_FRAMES and s[3] == 0 and s[2] == 0]
        assert starting, "no truthful 'starting' snapshot (not draining, first period queued, ALSA empty)"
        assert samples[-1][3] == 1, "the engine never reported playing"
        # Every starting snapshot says when the PCM is expected to drain (the
        # first start of the process uses the default estimate, ahead of the
        # snapshot); a draining snapshot carries none.
        assert all(s[5] > s[4] for s in starting), "starting snapshot without a start time"
        assert all(s[5] == 0 for s in samples if s[3] == 1), "playing snapshot with a start time"
        assert all(abs((s[5] - s[4]) / 1e6 - 1200) < 1000 for s in starting)
        assert len({s[5] for s in starting}) == 1, "the expected start time moved during one start"
    finally:
        rig.close()


def scenario_source_contract():
    text = (HERE / "audio_engine.c").read_text()
    loop = text[text.index("static int run_engine"):]
    inner = loop[loop.index("while (!stopping && sources_active(sources))"):]
    read = inner.index("read_sources(sources, root) < 0")
    drop = inner.index("drop_airplay_downstream(pcm, sources);")
    ready = inner.index("if (!period_ready(sources))")
    write = inner.index("write_period(pcm, output")
    assert read < drop < ready < write, "the drop must run between read_sources and the next write"
    assert re.search(r"g_airplay_drop_pending = 0;\n\t\tairplay_delay_publish\(NULL, sources\);\n\t\t/\* The clock domain",
                     loop), "a fresh PCM must clear the pending drop"
    assert loop.count("airplay_delay_publish(NULL, sources);") >= 7, "start path publishes"
    # The expected start time is set before the first starting publish and is
    # cleared on every way out of the start path: each failure, the idle
    # publish, the first playing publish and the end of the playback loop.
    start = loop.index("g_output_start_due_ns = start_began_ns + g_output_start_est_ns;")
    first_playing = loop.index("airplay_delay_publish(pcm, sources);", start)
    block = loop[start:first_playing]
    assert block.index("airplay_delay_publish(NULL, sources);") > 0
    for exit_point in ("output arm failed", "unavailable: %s", "playback start failed"):
        at = block.index(exit_point)
        assert "g_output_start_due_ns = 0;" in block[at:at + 400], f"{exit_point} must clear the start time"
    assert block.count("continue;") == 3, "a new exit from the start path must clear the start time"
    assert block.count("g_output_start_due_ns = 0;") == 3 + 1  # three failures + the success path
    assert "g_output_start_due_ns = 0;\n\t\tairplay_delay_publish(pcm, sources);" in loop[start:]
    top = loop.index("while (!stopping) {")
    assert "g_output_start_due_ns = 0;\n\t\tairplay_delay_publish(NULL, sources);\n\t\tif (poll_sources_ex" \
        in loop[top:start], "the idle publish must carry no start time"
    after_loop = loop[loop.index("SENDSPIN_TRACE(\"close\")") - 120:loop.index("SENDSPIN_TRACE(\"close\")")]
    assert "g_output_start_due_ns = 0;" in after_loop
    publish = text[text.index("static void airplay_delay_publish"):text.index("static void output_start_measured")]
    assert "pcm ? 0 : g_output_start_due_ns" in publish, "only no-PCM publishes carry the start time"
    body = text[text.index("static void drop_airplay_downstream"):text.index("static int service_airplay_reset")]
    assert "pcm_stop(pcm)" in body and "pcm_prepare(pcm)" in body
    for other in ("idle_periods > 0", "have_generation", "sendspin_live"):
        assert other in body, f"drop must be skipped for a shared ring ({other})"


def run():
    scenario_source_contract()
    with tempfile.TemporaryDirectory(prefix="le-session-end-") as temp:
        directory = Path(temp)
        scenario_session_end_drops_ring(directory)
        print("engine: session end drops the ALSA ring, nothing plays after it")
        scenario_replacement_drops_ring_and_fences(directory)
        print("engine: replacement drops the old ring; the new session is never cut")
        scenario_flush_without_boundary_keeps_ring(directory)
        print("engine: a flush with the marker untouched keeps the ring (skip/seek unchanged)")
        scenario_shared_ring_is_not_dropped(directory)
        print("engine: a ring shared with another bus is never dropped")
        scenario_start_path_keeps_the_page_fresh(directory)
        print("engine: delay page stays fresh through the slow start path")
    print("AirPlay session end drop and start-path publishing: PASS")


if __name__ == "__main__":
    run()

#!/usr/bin/env python3
"""AirPlay output-delay channel: math, staleness, engine publisher, end to end.

* airplay_delay.h arithmetic and safety (decay, clamping, engine restart
  re-basing, stale/unknown inputs, seqlock, fixed page size);
* the engine's publisher fed a fake ALSA ring;
* the real bridge + a real engine read loop + the patched Shairport backend:
  with the engine held, every frame written is accounted for exactly once
  across pipe + bridge + media FIFO + engine queue, and a stalled engine makes
  delay() fail instead of reporting a stale number;
* flush() (skip/seek/pause): with the same real bridge and engine, a flush
  empties everything queued past Shairport (pipe, bridge, media FIFO, engine
  AirPlay queue) so delay() drops to the new queue, the volume file and the
  session marker stay untouched, the engine keeps playing AirPlay (the next
  audio is the new track's, not the old tail), a flush racing play() from a
  second thread (the AirPlay 2 pause path) does not hang, and the counters
  appear in the fixed-size airplay.status.
"""
import os
import struct
import subprocess
import tempfile
import time
from pathlib import Path

import test_airplay_generation_fence as fence
import test_shairport_pipe_delay_patch as patchtest
from test_airplay_session_dsp import ENGINE_TEST, HERE
from test_audio_period_buffer import MIXER_HEADER, PCM_HEADER

CC = os.environ.get("CC", "cc")

MATH_TEST = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include "airplay_delay.h"

static struct le_ad_engine engine(uint64_t at, uint64_t read, uint32_t q, uint32_t a,
                                  int valid, int playing)
{
    struct le_ad_engine e;
    memset(&e, 0, sizeof(e));
    e.magic = LE_AD_MAGIC_ENGINE; e.epoch = 77; e.mono_ns = at;
    e.media_read_bytes = read; e.queue_frames = q; e.alsa_frames = a;
    e.alsa_valid = valid; e.playing = playing;
    return e;
}

int main(void)
{
    struct le_ad_bridge_ctx ctx;
    struct le_ad_parts p;
    struct le_ad_bridge b;
    struct le_ad_engine e;
    const uint64_t ms = 1000000ULL, t0 = 5000ULL * ms;
    long d;

    assert(sizeof(struct le_ad_engine) <= LE_AD_PAGE_SIZE);
    assert(sizeof(struct le_ad_bridge) <= LE_AD_PAGE_SIZE);
    assert(sizeof(struct le_ad_read) <= LE_AD_PAGE_SIZE);
    memset(&ctx, 0, sizeof(ctx));

    /* First sight of an engine re-bases the byte counters on the live FIFO. */
    e = engine(t0, 1000000, 2048, 3000, 1, 0);
    assert(le_ad_downstream(&ctx, &e, t0, 400, 4000, 100, &p) == LE_AD_OK);
    assert(p.fifo_frames == 1000 && p.pend_frames == 100);
    assert(p.downstream_frames == 100 + 1000 + 2048 + 3000);
    /* More bytes written, none read: the FIFO grows by exactly that. */
    assert(le_ad_downstream(&ctx, &e, t0, 400 + 8192, 4000 + 8192, 0, &p) == LE_AD_OK);
    assert(p.fifo_frames == 1000 + 2048);
    /* The engine reads 2000 bytes: they leave the FIFO and (in a later
     * snapshot) appear in its queue; the total is unchanged. */
    e = engine(t0 + 5 * ms, 1000000 + 2000, 2048 + 500, 3000, 1, 0);
    assert(le_ad_downstream(&ctx, &e, t0 + 5 * ms, 400 + 8192, 4000 + 8192 - 2000, 0, &p)
           == LE_AD_OK);
    assert(p.downstream_frames == (1000 + 2048 - 500) + 2548 + 3000);

    /* Draining: the sum decays at 48 kHz with the snapshot's age... */
    e = engine(t0, 1000000, 2000, 3000, 1, 1);
    memset(&ctx, 0, sizeof(ctx));
    assert(le_ad_downstream(&ctx, &e, t0 + 50 * ms, 0, 0, 0, &p) == LE_AD_OK);
    assert(p.downstream_frames == 5000 - 2400);
    /* ...never below zero, and never when nothing is draining. */
    assert(le_ad_downstream(&ctx, &e, t0 + 200 * ms, 0, 0, 0, &p) == LE_AD_OK);
    assert(p.downstream_frames == 0);
    e.playing = 0;
    assert(le_ad_downstream(&ctx, &e, t0 + 200 * ms, 0, 0, 0, &p) == LE_AD_OK);
    assert(p.downstream_frames == 5000);

    /* Untrustworthy engine snapshots are refused, not guessed at. */
    e = engine(t0, 0, 100, 100, 1, 1);
    assert(le_ad_downstream(&ctx, &e, t0 + 251 * ms, 0, 0, 0, &p) == LE_AD_ENGINE_STALE);
    e = engine(t0, 0, 100, 0, 0, 1);
    assert(le_ad_downstream(&ctx, &e, t0 + 5 * ms, 0, 0, 0, &p) == LE_AD_ALSA_UNKNOWN);
    e = engine(t0, 0, 100, 0, 0, 0);  /* idle: ALSA level irrelevant */
    assert(le_ad_downstream(&ctx, &e, t0 + 5 * ms, 0, 0, 0, &p) == LE_AD_OK);

    /* Engine restart: new epoch, counters restart from zero, FIFO re-based. */
    memset(&ctx, 0, sizeof(ctx));
    e = engine(t0, 50000, 0, 0, 1, 0);
    assert(le_ad_downstream(&ctx, &e, t0, 70000, 0, 0, &p) == LE_AD_OK && p.fifo_frames == 0);
    e = engine(t0 + ms, 0, 0, 0, 1, 0); e.epoch = 78;
    assert(le_ad_downstream(&ctx, &e, t0 + ms, 71000, 4000, 0, &p) == LE_AD_OK);
    assert(p.fifo_frames == 1000);
    assert(le_ad_downstream(&ctx, &e, t0 + ms, 71000 + 400, 4400, 0, &p) == LE_AD_OK);
    assert(p.fifo_frames == 1100);
    /* A corrupt offset (FIFO drained behind our back) re-bases on FIONREAD. */
    assert(le_ad_downstream(&ctx, &e, t0 + ms, 71400 + 900000, 0, 0, &p) == LE_AD_OK);
    assert(p.fifo_frames == 0);

    /* Shairport side: pipe + downstream, decayed only while draining. */
    memset(&b, 0, sizeof(b));
    b.magic = LE_AD_MAGIC_BRIDGE; b.mono_ns = t0; b.valid = 1; b.downstream_frames = 4800;
    assert(le_ad_delay_from_snapshot(&b, t0 + 10 * ms, 300, &d) == LE_AD_OK && d == 5100);
    b.playing = 1;
    assert(le_ad_delay_from_snapshot(&b, t0 + 10 * ms, 300, &d) == LE_AD_OK && d == 300 + 4800 - 480);
    assert(le_ad_delay_from_snapshot(&b, t0 + 249 * ms, 0, &d) == LE_AD_OK && d == 0);
    assert(le_ad_delay_from_snapshot(&b, t0 + 251 * ms, 0, &d) == LE_AD_STALE);
    b.valid = 0; b.reason = LE_AD_ENGINE_STALE;
    assert(le_ad_delay_from_snapshot(&b, t0, 0, &d) == LE_AD_ENGINE_STALE);
    b.reason = 0;
    assert(le_ad_delay_from_snapshot(&b, t0, 0, &d) == LE_AD_NO_ENGINE);
    /* A snapshot from the "future" (clock step) is age zero, not garbage. */
    b.valid = 1;
    assert(le_ad_delay_from_snapshot(&b, t0 - 5 * ms, 0, &d) == LE_AD_OK && d == 4800);

    /* Seqlock: odd means "being written"; end() always lands on even. */
    {
        uint32_t page[16] = {0};
        struct le_ad_engine copy;
        page[0] = LE_AD_MAGIC_ENGINE;
        assert(le_ad_read_page(page, &copy, sizeof(copy), LE_AD_MAGIC_ENGINE) == 0);
        le_ad_write_begin(page);
        assert(le_ad_write_in_progress(page));
        assert(le_ad_read_page(page, &copy, sizeof(copy), LE_AD_MAGIC_ENGINE) != 0);
        le_ad_write_begin(page); /* begin twice (stuck writer) stays odd */
        le_ad_write_end(page);
        assert(!le_ad_write_in_progress(page));
        assert(le_ad_read_page(page, &copy, sizeof(copy), LE_AD_MAGIC_ENGINE) == 0);
    }

    /* A page caught mid-update is BUSY, not absent; the retries back off
     * (bounded, a few ms at most) instead of spinning back to back. */
    {
        uint32_t page[16] = {0};
        struct le_ad_engine copy;
        uint64_t t;
        page[0] = LE_AD_MAGIC_ENGINE;
        le_ad_write_begin(page);
        t = le_ad_now_ns();
        assert(le_ad_read_page(page, &copy, sizeof(copy), LE_AD_MAGIC_ENGINE) == LE_AD_READ_BUSY);
        t = le_ad_now_ns() - t;
        assert(t >= 500000ULL && t < 50 * ms);
        le_ad_write_end(page);
        /* Wrong magic on a stable page is ABSENT: the page is not ours. */
        assert(le_ad_read_page(page, &copy, sizeof(copy), LE_AD_MAGIC_BRIDGE) == LE_AD_READ_ABSENT);
    }
    /* The bridge tick distinguishes the two: busy engine, absent engine. */
    {
        char path[] = "/tmp/le-ad-busy-XXXXXX";
        struct le_ad_bridge_ctx bridge;
        struct le_ad_engine_pub pub;
        struct le_ad_parts parts;
        int fd = mkstemp(path);
        assert(fd >= 0); close(fd);
        memset(&pub, 0, sizeof(pub));
        assert(le_ad_engine_open(&pub, path) == 0);
        le_ad_engine_publish(&pub, le_ad_now_ns(), 0, 0, 0, 1, 0);
        le_ad_bridge_open(&bridge, "/tmp/le-ad-busy-bridge-page", path);
        assert(le_ad_bridge_update(&bridge, le_ad_now_ns(), 0, 0, 0, &parts) == LE_AD_OK);
        le_ad_write_begin(pub.page);  /* the engine "is publishing" for good */
        assert(le_ad_bridge_update(&bridge, le_ad_now_ns(), 0, 0, 0, &parts) == LE_AD_ENGINE_BUSY);
        assert(strcmp(le_ad_reason_name(LE_AD_ENGINE_BUSY), "engine-busy") == 0);
        le_ad_write_end(pub.page);
        assert(le_ad_bridge_update(&bridge, le_ad_now_ns(), 0, 0, 0, &parts) == LE_AD_OK);
        le_ad_bridge_close(&bridge);
        le_ad_engine_close(&pub);
        unlink(path);
        unlink("/tmp/le-ad-busy-bridge-page");
        bridge.page = NULL;
        memset(&bridge, 0, sizeof(bridge));
        le_ad_bridge_open(&bridge, "/tmp/le-ad-busy-bridge-page", "/nonexistent/engine-page");
        assert(le_ad_bridge_update(&bridge, le_ad_now_ns(), 0, 0, 0, &parts) == LE_AD_NO_ENGINE);
        le_ad_bridge_close(&bridge);
        unlink("/tmp/le-ad-busy-bridge-page");
    }
    puts("math ok");
    return 0;
}
'''

PUBLISH_TEST = r'''
#define main engine_main
#include "audio_engine.c"
#undef main
#include <assert.h>

struct pcm { unsigned int avail; int fail; long long age_ns; };
unsigned int pcm_get_buffer_size(const struct pcm *pcm) { (void)pcm; return 4096; }
int pcm_get_htimestamp(struct pcm *pcm, unsigned int *avail, struct timespec *ts)
{
    uint64_t at;
    if (pcm->fail) return -1;
    at = le_ad_now_ns() - (uint64_t)pcm->age_ns;
    ts->tv_sec = (time_t)(at / 1000000000ULL); ts->tv_nsec = (long)(at % 1000000000ULL);
    *avail = pcm->avail;
    return 0;
}
struct mixer *mixer_open(unsigned int card) { (void)card; return NULL; }
void mixer_close(struct mixer *m) { (void)m; }
struct mixer_ctl *mixer_get_ctl_by_name(struct mixer *m, const char *n) { (void)m; (void)n; return NULL; }
unsigned int mixer_ctl_get_num_values(struct mixer_ctl *c) { (void)c; return 0; }
int mixer_ctl_set_value(struct mixer_ctl *c, unsigned int i, int v) { (void)c; (void)i; (void)v; return -1; }
int mixer_ctl_get_value(struct mixer_ctl *c, unsigned int i) { (void)c; (void)i; return -1; }

static struct le_ad_engine snap(const char *path)
{
    struct le_ad_reader r; struct le_ad_engine e;
    memset(&r, 0, sizeof(r));
    assert(le_ad_reader_get(&r, path, 0));
    assert(le_ad_read_page(r.map, &e, sizeof(e), LE_AD_MAGIC_ENGINE) == 0);
    le_ad_reader_close(&r);
    return e;
}

int main(int argc, char **argv)
{
    struct source_bus buses[SOURCE_COUNT];
    struct pcm pcm = { 4096 - 1500, 0, 5000000 };
    char path[256], fifo[256];
    struct le_ad_engine e;
    uint64_t before;
    int fd;
    unsigned char junk[1000] = {0};
    assert(argc == 2);
    memset(buses, 0, sizeof(buses));
    assert(setup_sources(buses, argv[1]) == 0);
    snprintf(path, sizeof(path), "%s/" AIRPLAY_ENGINE_DELAY_FILE, argv[1]);
    assert(le_ad_engine_open(&g_airplay_delay, path) == 0);

    buses[SOURCE_AIRPLAY].received = 2048 * 4;
    before = le_ad_now_ns();
    airplay_delay_publish(&pcm, buses);
    e = snap(path);
    assert(e.queue_frames == 2048 && e.alsa_frames == 1500 && e.alsa_valid && e.playing);
    assert(e.mono_ns <= le_ad_now_ns() && e.mono_ns < before);  /* ALSA's own stamp (5 ms old) */
    assert(before - e.mono_ns >= 4000000ULL && before - e.mono_ns < 100000000ULL);

    /* An AirPlay session still waiting for its volume ack is held, not played. */
    buses[SOURCE_AIRPLAY].airplay_volume_missing = 1;
    airplay_delay_publish(&pcm, buses);
    e = snap(path);
    assert(!e.playing && e.queue_frames == 2048);
    buses[SOURCE_AIRPLAY].airplay_volume_missing = 0;

    /* PCM closed: nothing queued in ALSA, nothing draining. */
    airplay_delay_publish(NULL, buses);
    e = snap(path);
    assert(!e.playing && e.alsa_frames == 0 && e.alsa_valid);

    /* A failed ALSA query is reported as unknown, never as zero. */
    pcm.fail = 1;
    airplay_delay_publish(&pcm, buses);
    e = snap(path);
    assert(e.playing && !e.alsa_valid);

    /* Bytes taken from the FIFO by a discard are counted as read. */
    snprintf(fifo, sizeof(fifo), "%s/airplay-media.pcm", argv[1]);
    fd = open(fifo, O_WRONLY | O_NONBLOCK); assert(fd >= 0);
    assert(write(fd, junk, sizeof(junk)) == (ssize_t)sizeof(junk));
    close(fd);
    assert(discard_disconnected_media(&buses[SOURCE_AIRPLAY]) == 0);
    airplay_delay_publish(NULL, buses);
    e = snap(path);
    assert(e.media_read_bytes == 1000);
    assert(e.epoch == g_airplay_delay.epoch && e.epoch != 0);
    close_sources(buses);
    le_ad_engine_close(&g_airplay_delay);
    puts("publish ok");
    return 0;
}
'''

ENGINE_LOOP = fence.ENGINE_DRIVER.replace(
    'int main(int argc, char **argv)',
    'unsigned int pcm_get_buffer_size(const struct pcm *p) { (void)p; return 4096; }\n'
    'int pcm_get_htimestamp(struct pcm *p, unsigned int *a, struct timespec *t)\n'
    '{ (void)p; (void)a; (void)t; return -1; }\n'
    'int main(int argc, char **argv)',
).replace(
    'assert(setup_sources(buses, argv[1]) == 0);',
    'assert(setup_sources(buses, argv[1]) == 0);\n'
    '    {\n'
    '        char page[300];\n'
    '        snprintf(page, sizeof(page), "%s/" AIRPLAY_ENGINE_DELAY_FILE, argv[1]);\n'
    '        assert(le_ad_engine_open(&g_airplay_delay, page) == 0);\n'
    '    }',
).replace(
    'assert(poll_sources(buses, 20) >= 0);',
    'if (access("nopub", F_OK) != 0) airplay_delay_publish(NULL, buses);\n'
    '        assert(poll_sources(buses, 20) >= 0);',
).replace(
    'if (source_period_ready(&buses[SOURCE_AIRPLAY])) {',
    'if (source_period_ready(&buses[SOURCE_AIRPLAY]) && access("hold", F_OK) != 0) {',
)
assert "airplay_delay_publish" in ENGINE_LOOP and '"hold"' in ENGINE_LOOP


def wait_for(predicate, timeout=5.0, message="timed out"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(message)


def read_bridge_page(path):
    raw = path.read_bytes()
    for _ in range(50):
        magic, seq, at, down, playing, valid, reason, pend, fifo, queue, alsa = \
            struct.unpack("<IIQIIIIIIII", raw[:48])
        if seq % 2 == 0:
            return dict(seq=seq, at=at, down=down, playing=playing, valid=valid, reason=reason,
                        pend=pend, fifo=fifo, queue=queue, alsa=alsa)
        raw = path.read_bytes()
    raise AssertionError("bridge page stuck mid-update")


def compile_c(directory, name, source, *extra):
    (directory / f"{name}.c").write_text(source)
    subprocess.run([CC, "-std=c99", "-Wall", "-Wextra", "-Werror", "-ffunction-sections",
                    "-fdata-sections", "-I", str(directory), "-I", str(HERE),
                    str(directory / f"{name}.c"), *extra, "-Wl,--gc-sections", "-lm",
                    "-o", str(directory / name)], check=True, timeout=90)


def unit_tests(directory):
    compile_c(directory, "math", MATH_TEST)
    out = subprocess.run([str(directory / "math")], check=True, timeout=20,
                         capture_output=True, text=True).stdout
    assert "math ok" in out
    (directory / "tinyalsa").mkdir()
    (directory / "tinyalsa/mixer.h").write_text(MIXER_HEADER)
    (directory / "tinyalsa/pcm.h").write_text(PCM_HEADER)
    bus = directory / "bus"
    bus.mkdir()
    compile_c(directory, "publish", PUBLISH_TEST, str(HERE / "audio_sink.c"),
              str(HERE / "audio_timing.c"))
    out = subprocess.run([str(directory / "publish"), str(bus)], check=True, timeout=20,
                         capture_output=True, text=True).stdout
    assert "publish ok" in out


def end_to_end(directory):
    """Real bridge + engine read loop + patched Shairport backend."""
    fence.ENGINE_DRIVER, saved = ENGINE_LOOP, fence.ENGINE_DRIVER
    os.environ["LE_TEST_BRIDGE_CFLAGS"] = "-DEVENT_INTERVAL_MS=200ULL -DSTATUS_INTERVAL_MS=200ULL"
    try:
        root = directory / "e2e"
        root.mkdir()
        fence.compile_fixture(root)
    finally:
        fence.ENGINE_DRIVER = saved
        os.environ.pop("LE_TEST_BRIDGE_CFLAGS", None)
    shairport = patchtest.build_driver(directory / "shairport")
    log = root / "observed"
    (root / "hold").write_text("")  # the slow engine: reads 2 periods, then stops
    engine = subprocess.Popen([str(root / "engine"), str(root), str(log)], cwd=root)
    bridge = None
    driver = None
    try:
        fifo = root / "airplay-media.pcm"
        wait_for(fifo.exists)
        os.mkfifo(root / "input.pcm")
        bridge_log = open(root / "bridge.log", "w")
        bridge = subprocess.Popen([str(root / "bridge")], stderr=bridge_log)
        page = root / "input.pcm.delay"
        wait_for(page.exists)
        pipe_fd = os.open(root / "input.pcm", os.O_RDWR | os.O_NONBLOCK)
        # A real hook-started, volume-admitted session.
        assert fence.hook(root, "--start") == 0
        fence.admit(root)
        driver = patchtest.Driver(shairport, root / "input.pcm")
        assert driver.ask("prepare") == "prepare rc=0"

        frames = 20000  # past the engine queue, inside what the two FIFOs hold
        sent = 0
        while sent < frames:
            step = min(1408 // 4, frames - sent)
            assert driver.ask(f"play {step} {sent}") == "play rc=0"
            sent += step
            time.sleep(0.0005)

        def settled():
            page_now = read_bridge_page(page)
            rc, value = driver.delay()
            return (page_now, rc, value)

        def exact():
            page_now, rc, value = settled()
            return rc == 0 and value == frames and page_now["valid"]
        wait_for(exact, 6.0, "pipe + bridge + FIFO + engine queue never summed to every written "
                 f"frame; last={settled()}")
        page_now = read_bridge_page(page)
        assert page_now["queue"] > 0 and page_now["fifo"] > 0, page_now
        assert page_now["valid"] == 1 and page_now["reason"] == 0

        # Release the engine: the figure drains to zero with nothing lost.
        (root / "hold").unlink()
        wait_for(lambda: driver.delay() == (0, 0), 6.0, "delay never drained to zero")

        # A stalled engine (its snapshot stops advancing) must fail delay().
        engine.send_signal(19)  # SIGSTOP
        try:
            def refused():
                rc, _ = driver.delay()
                return rc != 0
            wait_for(refused, 3.0, "delay() kept answering with a stalled engine")
            assert read_bridge_page(page)["valid"] == 0
        finally:
            engine.send_signal(18)  # SIGCONT
        wait_for(lambda: driver.delay()[0] == 0, 3.0, "delay() did not recover with the engine")
        os.close(pipe_fd)
        bridge.terminate()
        bridge.wait(timeout=5)
        bridge = None
        text = (root / "bridge.log").read_text()
        assert "delay-fallback" in text and "engine-stale" in text, text  # stall is logged
    finally:
        if driver:
            driver.close()
        if bridge:
            bridge.terminate()
            try:
                bridge.wait(timeout=3)
            except subprocess.TimeoutExpired:
                bridge.kill()
        engine.terminate()
        try:
            engine.wait(timeout=3)
        except subprocess.TimeoutExpired:
            engine.kill()


def identity(path):
    st = path.stat()
    return (st.st_dev, st.st_ino, st.st_ctime_ns, st.st_mtime_ns, path.read_bytes())


def status_fields(path):
    raw = path.read_bytes()
    assert len(raw) == 1024 and raw.endswith(b"\n"), len(raw)
    return dict(line.split("=", 1) for line in raw.decode().splitlines() if "=" in line)


def end_to_end_flush(directory):
    """Real bridge + engine read loop + patched Shairport: skip/seek/pause flush."""
    fence.ENGINE_DRIVER, saved = ENGINE_LOOP, fence.ENGINE_DRIVER
    os.environ["LE_TEST_BRIDGE_CFLAGS"] = "-DEVENT_INTERVAL_MS=200ULL -DSTATUS_INTERVAL_MS=200ULL"
    try:
        root = directory / "e2e-flush"
        root.mkdir()
        fence.compile_fixture(root)
    finally:
        fence.ENGINE_DRIVER = saved
        os.environ.pop("LE_TEST_BRIDGE_CFLAGS", None)
    shairport = patchtest.build_driver(directory / "shairport-flush")
    log = root / "observed"
    (root / "hold").write_text("")  # the engine reads into its queue but plays nothing
    engine = subprocess.Popen([str(root / "engine"), str(root), str(log)], cwd=root)
    bridge = None
    driver = None
    try:
        wait_for((root / "airplay-media.pcm").exists)
        os.mkfifo(root / "input.pcm")
        bridge = subprocess.Popen([str(root / "bridge")], stderr=open(root / "bridge.log", "w"))
        page = root / "input.pcm.delay"
        wait_for(page.exists)
        pipe_fd = os.open(root / "input.pcm", os.O_RDWR | os.O_NONBLOCK)
        assert fence.hook(root, "--start") == 0
        fence.admit(root)
        driver = patchtest.Driver(shairport, root / "input.pcm")
        assert driver.ask("prepare") == "prepare rc=0"
        marker = identity(root / "airplay.active")
        volume = identity(root / "airplay.volume")
        master = identity(root / "airplay.master")
        counters_path = Path(str(root / "input.pcm") + ".counters")

        # Old track: 20000 frames queued across pipe + bridge + FIFO + engine.
        old = 20000
        sent = 0
        while sent < old:
            step = min(1408 // 4, old - sent)
            assert driver.ask(f"play {step} {sent}") == "play rc=0"
            sent += step
            time.sleep(0.0005)
        wait_for(lambda: driver.delay() == (0, old), 6.0, "old audio never fully queued")
        queued = read_bridge_page(page)
        assert queued["queue"] > 0 and queued["fifo"] > 0, queued

        # Skip: flush() returns promptly, everything past Shairport is gone and
        # delay() reports the new, empty queue.
        started = time.monotonic()
        assert driver.ask("flush").startswith("flush ms=")
        assert time.monotonic() - started < 0.3, "flush() was not answered by the bridge"
        wait_for(lambda: driver.delay() == (0, 0), 3.0, "delay() did not drop to the emptied queue")
        emptied = read_bridge_page(page)
        assert (emptied["pend"], emptied["fifo"], emptied["queue"]) == (0, 0, 0), emptied
        assert emptied["valid"] == 1
        # Session state untouched: marker, volume file and the engine's master ack.
        assert identity(root / "airplay.active") == marker
        assert identity(root / "airplay.volume") == volume
        assert identity(root / "airplay.master") == master
        wait_for(lambda: "flush x" in (root / "bridge.log").read_text(), 3)
        text = (root / "bridge.log").read_text()
        assert "engine queue reset" in text and "engine NOT reset" not in text, text
        assert "session-end" not in text and "discard x" not in text, text

        # New track: exactly its own frames are queued, and once the engine plays
        # again its first period is the new track's first frame, not the old tail.
        new_base = 40000
        sent = 0
        while sent < 6000:
            step = min(1408 // 4, 6000 - sent)
            assert driver.ask(f"play {step} {new_base + sent}") == "play rc=0"
            sent += step
            time.sleep(0.0005)
        wait_for(lambda: driver.delay() == (0, 6000), 6.0, "new audio not accounted exactly")
        (root / "hold").unlink()
        first = wait_for(lambda: [w for w in log.read_text().split()
                                  if not w.startswith("partial:")] or None, 6.0,
                         "engine never played the new track (AirPlay left unadmitted?)")[0]
        expected = new_base & 0xFFFF
        expected = expected - 65536 if expected >= 32768 else expected
        assert int(first) == expected, f"first played period {first}, want the new track's {expected}"
        wait_for(lambda: driver.delay() == (0, 0), 6.0)

        # Slow engine: it acknowledges the reset but does not publish again for
        # a while. The bridge must answer within Shairport's wait, publish NO
        # figure (delay() -> -ENODEV, never the pre-reset queue) and recover
        # with the exact post-flush value as soon as the engine publishes.
        (root / "hold").write_text("")
        sent = 0
        while sent < 6000:
            step = min(1408 // 4, 6000 - sent)
            assert driver.ask(f"play {step} {sent}") == "play rc=0"
            sent += step
            time.sleep(0.0005)
        wait_for(lambda: driver.delay() == (0, 6000), 6.0, "6000 frames not queued")
        timeouts = patchtest.read_counters(counters_path)["flush_timeouts"]
        (root / "nopub").write_text("")
        time.sleep(0.15)  # the engine's last publish is now older than the flush
        started = time.monotonic()
        driver.ask("flush")
        took = time.monotonic() - started
        assert 0.1 <= took < 0.3, f"late-snapshot flush took {took:.3f}s"
        assert patchtest.read_counters(counters_path)["flush_timeouts"] == timeouts, \
            "the bridge must still answer within Shairport's wait"
        for _ in range(10):
            page_now = read_bridge_page(page)
            assert page_now["valid"] == 0 and page_now["reason"] == 9, page_now
            rc, _ = driver.delay()
            assert rc != 0, "delay() used pre-reset engine figures after a flush"
            time.sleep(0.01)
        (root / "nopub").unlink()
        wait_for(lambda: driver.delay() == (0, 0), 3.0,
                 "delay() did not recover to the exact post-flush value")
        assert read_bridge_page(page)["valid"] == 1
        assert driver.ask("play 1000 5") == "play rc=0"
        wait_for(lambda: driver.delay() == (0, 1000), 3.0)
        wait_for(lambda: status_fields(root / "airplay.status")["flush_engine_late"] == "1", 3.0,
                 "flush_engine_late missing from airplay.status")
        wait_for(lambda: "snapshot LATE" in (root / "bridge.log").read_text(), 3.0)
        (root / "hold").unlink()
        wait_for(lambda: driver.delay() == (0, 0), 6.0)

        # Pause path: AirPlay 2 flushes from its buffered-audio thread while the
        # player thread is writing. No hang, bounded, bridge answered the last one.
        counters = counters_path
        before = patchtest.read_counters(counters)
        text = driver.ask("race 100", timeout=30)
        assert int(text.split("ms=")[1]) < 25000, text
        after = patchtest.read_counters(counters)
        assert after["flush_calls"] == before["flush_calls"] + 100, (before, after)
        wait_for(lambda: struct.unpack("<I", (root / "input.pcm.delay").read_bytes()[48:52])[0]
                 == after["flush_req"], 3.0, "last flush request never answered")
        wait_for(lambda: driver.delay()[0] == 0, 3.0)

        # The counters reach the fixed-size status file.
        driver.ask("setctr 2 3 40 10")
        driver.delay()
        wait_for(lambda: status_fields(root / "airplay.status").get("shairport_resyncs") == "2",
                 3.0, "Shairport's counters never reached airplay.status")
        fields = status_fields(root / "airplay.status")
        assert (fields["shairport_late_drops"], fields["shairport_frames_inserted"],
                fields["shairport_frames_dropped"]) == ("3", "40", "10"), fields
        assert int(fields["flushes"]) >= 1 and int(fields["shairport_flush_calls"]) >= 101, fields
        assert fields["flush_engine_failed"] == "0" and fields["flush_expired"] == "0", fields
        assert fields["flush_engine_late"] == "1", fields
        os.close(pipe_fd)
    finally:
        if driver:
            driver.close()
        if bridge:
            bridge.terminate()
            try:
                bridge.wait(timeout=3)
            except subprocess.TimeoutExpired:
                bridge.kill()
        engine.terminate()
        try:
            engine.wait(timeout=3)
        except subprocess.TimeoutExpired:
            engine.kill()


def run():
    with tempfile.TemporaryDirectory(prefix="le-delay-channel-") as temp:
        directory = Path(temp)
        unit_tests(directory)
        end_to_end(directory)
        end_to_end_flush(directory)
    print("AirPlay output-delay channel (math, engine publisher, bridge, Shairport): PASS")


if __name__ == "__main__":
    run()

#!/usr/bin/env python3
"""The pipe-delay Shairport patch applies cleanly and exposes a working delay().

Applies shairport-5.6.2-pipe-delay.patch to the pinned upstream audio_pipe.c,
compiles the patched backend against minimal stand-ins for Shairport's
audio.h/common.h and drives its real init/prepare/play/delay entry points
against hand-built bridge pages: fresh, draining, stale, invalid, torn and
missing.  A failing delay() must be -ENODEV so Shairport falls back to its
no-delay() behaviour.

It also covers the flush() the patch adds (skip/seek/pause): a fake bridge
answers the flush request through the RAM pages, and the tests prove that
flush() and play() exclude each other (two threads, no deadlock, bounded
time), that a play() blocked on a full pipe cannot hold flush() off and drops
its stale remainder, that delay() after a flush reports the emptied queue, and
that the resync / late-drop / sync-correction counters reach the counters page.
"""
import hashlib
import os
import select
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures/shairport-5.6.2"
HOOK_PATCH = HERE / "shairport-5.6.2-session-hooks.patch"
PIPE_PATCH = HERE / "shairport-5.6.2-pipe-delay.patch"
UPSTREAM = ("common.c", "common.h", "player.c", "player.h", "audio_pipe.c")
PIPE_SHA256 = "27d5ce1a40923c559b2dcc058b5fc74c24b56d25c1babb0b2644c9c461ad4fa1"
# The pristine Shairport 5.6.2 files build_airplay.sh pins (sha256).
UPSTREAM_SHA256 = {
    "common.c": "7011bff6f49ddada8ce9b34ea36216611c3bfe1847222ca5b72e7798c922667f",
    "common.h": "66ef34c7bc1a5c4612786b4c8860da6769ae5e1d146800ccb3a5f5537b1eaa3e",
    "player.c": "18b5ee672237592d80d8b62e5a5d9236ae0d5537a6ecf47097dd6c586c360c33",
    "player.h": "df449b1074ec67da077631a26cb9d8ca5433474a60efe47d1b47bd4bb8a165a9",
    "audio_pipe.c": PIPE_SHA256,
}

MAGIC_BRIDGE = 0x3142454C
MAGIC_READ = 0x3152454C
MAGIC_COUNTERS = 0x3143454C
PAGE = 64
FLUSH_WAIT_S = 0.3
STALE, NO_ENGINE, ENGINE_STALE, RACE, NO_PAGE = 2, 3, 4, 7, 1

AUDIO_H = r'''
#ifndef STUB_AUDIO_H
#define STUB_AUDIO_H
#include <stdint.h>
typedef struct {
  const char *name;
  void (*help)(void);
  int (*init)(int, char **);
  void (*deinit)(void);
  int (*prepare)(void);
  int32_t (*get_configuration)(unsigned int, unsigned int, unsigned int);
  int (*configure)(int32_t, char **);
  void (*stop)(void);
  int (*is_running)(void);
  void (*flush)(void);
  int (*delay)(long *);
  int (*stats)(uint64_t *, uint64_t *, uint64_t *, uint64_t *);
  int (*play)(void *, int, int, uint32_t, uint64_t);
  void (*volume)(double);
  void *(*parameters)(void);
  int (*mute)(int);
} audio_output;
extern audio_output audio_pipe;
#endif
'''

COMMON_H = r'''
#ifndef STUB_COMMON_H
#define STUB_COMMON_H
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define CONFIG_AIRPLAY_2 1
enum { SPS_FORMAT_S16_LE = 1, SPS_FORMAT_S32_LE = 2 };
enum { SPS_RATE_44100 = 1, SPS_RATE_48000 = 2 };
struct stub_config {
  void *cfg;
  double audio_backend_buffer_desired_length;
  double audio_backend_latency_offset;
};
extern struct stub_config config;
#define debug(...) ((void)0)
#define warn(...) ((void)0)
#define die(...) do { fprintf(stderr, __VA_ARGS__); exit(99); } while (0)
#define FORMAT_FROM_ENCODED_FORMAT(x) ((x) & 0xff)
#define CHANNELS_FROM_ENCODED_FORMAT(x) (((x) >> 16) & 0xff)
void parse_audio_options(const char *, uint32_t, uint32_t, uint32_t);
int config_lookup_non_empty_string(void *, const char *, const char **);
int try_to_open_pipe_for_writing(const char *);
void safe_socket_close(int *);
int32_t search_for_suitable_configuration(unsigned int, unsigned int, unsigned int, void *);
unsigned int sps_format_sample_size(unsigned int);
#endif
'''

DRIVER = r'''
#define _GNU_SOURCE
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include "audio.h"
#include "common.h"

struct stub_config config;
void parse_audio_options(const char *n, uint32_t f, uint32_t r, uint32_t c)
{ (void)n; (void)f; (void)r; (void)c; }
int config_lookup_non_empty_string(void *c, const char *k, const char **s)
{ (void)c; (void)k; (void)s; return 0; }
int try_to_open_pipe_for_writing(const char *path)
{
  int fd = open(path, O_WRONLY | O_NONBLOCK);
  if (fd >= 0) fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) & ~O_NONBLOCK);
  return fd;
}
void safe_socket_close(int *fd) { if (*fd >= 0) close(*fd); *fd = -1; }
int32_t search_for_suitable_configuration(unsigned int c, unsigned int r, unsigned int f, void *x)
{ (void)x; return (int32_t)(f | (c << 16) | (r << 8)); }
unsigned int sps_format_sample_size(unsigned int f) { return f == SPS_FORMAT_S32_LE ? 4 : 2; }

/* Counters player.c defines in the real build. */
uint32_t le_ad_ctr_resyncs, le_ad_ctr_late_drops, le_ad_ctr_frames_inserted,
    le_ad_ctr_frames_dropped;

struct bg { unsigned int n, v; int rc; };
static pthread_t bg_thread;
static struct bg bg_args;
static int bg_running;
static void *bg_play(void *arg)
{
  struct bg *b = arg;
  unsigned int *buf = malloc(b->n * 4), i;
  for (i = 0; i < b->n; ++i) buf[i] = b->v + i;
  b->rc = audio_pipe.play(buf, (int)b->n, 0, 0, 0);
  free(buf);
  return NULL;
}
struct race { int iterations; };
static void *race_play(void *arg)
{
  struct race *r = arg;
  unsigned int buf[64], i, k;
  for (k = 0; k < (unsigned int)r->iterations; ++k) {
    for (i = 0; i < 64; ++i) buf[i] = k * 64 + i;
    audio_pipe.play(buf, 64, 0, 0, 0);
  }
  return NULL;
}
static void *race_flush(void *arg)
{
  struct race *r = arg;
  int k;
  for (k = 0; k < r->iterations; ++k) audio_pipe.flush();
  return NULL;
}
static void *ex_flush(void *arg)
{
  (void)arg;
  audio_pipe.flush();
  return NULL;
}
static long now_ms(void)
{
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

int main(int argc, char **argv)
{
  char line[160];
  char *args[1];
  if (argc != 2) return 2;
  args[0] = argv[1];
  config.audio_backend_buffer_desired_length = -1;
  if (audio_pipe.init(1, args) != 0) return 3;
  printf("desired=%.3f has_delay=%d has_flush=%d\n", config.audio_backend_buffer_desired_length,
         audio_pipe.delay != NULL, audio_pipe.flush != NULL);
  /* S16_LE, 2 channels: 4 bytes per frame, as in airplay2.conf. */
  if (audio_pipe.configure((int32_t)(SPS_FORMAT_S16_LE | (2 << 16)), NULL) != 0) return 4;
  fflush(stdout);
  while (fgets(line, sizeof(line), stdin)) {
    unsigned int n = 0, v = 0, x = 0, y = 0;
    if (!strncmp(line, "prepare", 7)) {
      printf("prepare rc=%d\n", audio_pipe.prepare());
    } else if (sscanf(line, "play %u %u", &n, &v) == 2) {
      unsigned int *buf = malloc(n * 4), i;
      for (i = 0; i < n; ++i) buf[i] = v + i;
      printf("play rc=%d\n", audio_pipe.play(buf, (int)n, 0, 0, 0));
      free(buf);
    } else if (!strncmp(line, "delay", 5)) {
      long d = -12345;
      int rc = audio_pipe.delay(&d);
      printf("delay rc=%d value=%ld\n", rc, rc == 0 ? d : -1L);
    } else if (!strncmp(line, "flush", 5)) {
      long t = now_ms();
      audio_pipe.flush();
      printf("flush ms=%ld\n", now_ms() - t);
    } else if (sscanf(line, "setctr %u %u %u %u", &n, &v, &x, &y) == 4) {
      le_ad_ctr_resyncs = n; le_ad_ctr_late_drops = v;
      le_ad_ctr_frames_inserted = x; le_ad_ctr_frames_dropped = y;
      printf("setctr ok\n");
    } else if (sscanf(line, "bgplay %u %u", &n, &v) == 2) {
      bg_args.n = n; bg_args.v = v; bg_args.rc = -99;
      bg_running = pthread_create(&bg_thread, NULL, bg_play, &bg_args) == 0;
      printf("bgplay started=%d\n", bg_running);
    } else if (!strncmp(line, "bgwait", 6)) {
      if (bg_running) pthread_join(bg_thread, NULL);
      bg_running = 0;
      printf("bgwait rc=%d\n", bg_args.rc);
    } else if (sscanf(line, "race %u", &n) == 1) {
      struct race r = { (int)n };
      pthread_t a, b;
      long t = now_ms();
      pthread_create(&a, NULL, race_play, &r);
      pthread_create(&b, NULL, race_flush, &r);
      pthread_join(a, NULL);
      pthread_join(b, NULL);
      printf("race done ms=%ld\n", now_ms() - t);
    } else if (!strncmp(line, "exclusion", 9)) {
      /* flush() waits for an unanswered bridge while holding the I/O mutex;
       * a play() started meanwhile has to wait for it. */
      pthread_t t;
      unsigned int buf[10] = {0};
      long t0;
      pthread_create(&t, NULL, ex_flush, NULL);
      usleep(50000);
      t0 = now_ms();
      audio_pipe.play(buf, 10, 0, 0, 0);
      printf("exclusion play_ms=%ld\n", now_ms() - t0);
      pthread_join(t, NULL);
    } else if (!strncmp(line, "quit", 4)) {
      break;
    }
    fflush(stdout);
  }
  return 0;
}
'''


def apply_patches(tree: Path, only_pipe=False):
    for name in UPSTREAM:
        (tree / name).write_bytes((FIXTURE / name).read_bytes())
    patches = [PIPE_PATCH] if only_pipe else [HOOK_PATCH, PIPE_PATCH]
    for patch in patches:
        subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(patch)],
                       cwd=tree, check=True, timeout=30, stdout=subprocess.DEVNULL)


def build_driver(directory: Path) -> Path:
    """Patch the pinned audio_pipe.c and build a driver around it."""
    tree = directory / "tree"
    stub = directory / "stub"
    (stub / "utilities").mkdir(parents=True)
    tree.mkdir()
    (stub / "audio.h").write_text(AUDIO_H)
    (stub / "common.h").write_text(COMMON_H)
    (stub / "utilities/network_utilities.h").write_text("")
    apply_patches(tree, only_pipe=True)
    # The fixtures hold only the files the hook patch edits, not Shairport's
    # real headers: compile the patched backend alone, beside the shared header
    # that build_airplay.sh installs next to it.
    source = directory / "source"
    source.mkdir()
    (source / "audio_pipe.c").write_bytes((tree / "audio_pipe.c").read_bytes())
    (source / "airplay_delay.h").write_bytes((HERE / "airplay_delay.h").read_bytes())
    (directory / "driver.c").write_text(DRIVER)
    binary = directory / "driver"
    subprocess.run([os.environ.get("CC", "cc"), "-std=gnu99", "-Wall", "-Wextra", "-Werror",
                    "-Wno-unused-parameter", "-I", str(stub), "-I", str(source),
                    str(directory / "driver.c"), str(source / "audio_pipe.c"),
                    "-lpthread", "-o", str(binary)], check=True, timeout=60)
    return binary


class Driver:
    def __init__(self, binary: Path, pipe: Path):
        self.process = subprocess.Popen([str(binary), str(pipe)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, text=True)
        self.banner = self.process.stdout.readline().strip()

    def ask(self, line: str, timeout: float = 20.0) -> str:
        """One command, one reply line; a hang fails the test instead of the run."""
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        if not ready:
            self.process.kill()
            raise AssertionError(f"driver did not answer {line!r} within {timeout} s (deadlock?)")
        return self.process.stdout.readline().strip()

    def delay(self):
        text = self.ask("delay")
        rc = int(text.split("rc=")[1].split()[0])
        value = int(text.split("value=")[1])
        return rc, value

    def close(self):
        try:
            self.process.stdin.write("quit\n")
            self.process.stdin.flush()
        except OSError:
            pass
        self.process.wait(timeout=10)


def bridge_page(path: Path, *, downstream=0, playing=0, valid=1, reason=0, at=None,
                seq=2, magic=MAGIC_BRIDGE, flush_done=0, start=0):
    at = time.monotonic_ns() if at is None else at
    raw = struct.pack("<IIQIIIIIIIIIIII", magic, seq, at, downstream, playing, valid,
                      reason, 0, 0, 0, 0, flush_done, 0, 0, start)
    path.write_bytes(raw.ljust(PAGE, b"\0"))


def read_counters(path: Path):
    raw = path.read_bytes()
    assert len(raw) == PAGE, f"counters page must stay {PAGE} bytes, got {len(raw)}"
    (magic, seq, at, flush_req, flush_calls, flush_timeouts, resyncs, late, inserted,
     dropped, _) = struct.unpack("<IIQ8I", raw[:48])
    assert magic == MAGIC_COUNTERS and seq % 2 == 0, (hex(magic), seq)
    return {"at": at, "flush_req": flush_req, "flush_calls": flush_calls,
            "flush_timeouts": flush_timeouts, "resyncs": resyncs, "late_drops": late,
            "frames_inserted": inserted, "frames_dropped": dropped}


class FakeBridge(threading.Thread):
    """Plays the bridge's side of the pages: keeps its snapshot fresh and, when
    allowed, answers a flush request the way the real bridge does (drain the
    pipe, publish zero downstream, set flush_done)."""

    def __init__(self, root: Path, reader_fd: int, ack=True, downstream=0):
        super().__init__(daemon=True)
        self.page = root / "airplay.pcm.delay"
        self.counters = root / "airplay.pcm.counters"
        self.reader_fd = reader_fd
        self.ack = ack
        self.downstream = downstream
        self.done = 0
        self.drained = 0
        self.seen_requests = []
        self.stop_flag = False
        self.seq = 0
        self.fd = os.open(self.page, os.O_RDWR | os.O_CREAT, 0o644)
        os.ftruncate(self.fd, PAGE)
        self.publish()

    def publish(self):
        body = struct.pack("<IQIIIIIIIIIIII", MAGIC_BRIDGE, time.monotonic_ns(), self.downstream,
                           0, 1, 0, 0, 0, 0, 0, self.done, 0, 0, 0)
        # Seqlock write in place (never truncate a page a reader has mapped).
        self.seq += 2
        os.pwrite(self.fd, struct.pack("<I", self.seq - 1), 4)
        os.pwrite(self.fd, body[:4], 0)
        os.pwrite(self.fd, body[4:], 8)
        os.pwrite(self.fd, struct.pack("<I", self.seq), 4)

    def request(self):
        try:
            return read_counters(self.counters)["flush_req"]
        except (OSError, AssertionError, struct.error):
            return None

    def run(self):
        while not self.stop_flag:
            req = self.request()
            if self.ack and req is not None and req != self.done:
                self.seen_requests.append(req)
                try:
                    while True:
                        self.drained += len(os.read(self.reader_fd, 65536))
                except BlockingIOError:
                    pass
                self.downstream = 0
                self.done = req
            self.publish()
            time.sleep(0.003)

    def close(self):
        self.stop_flag = True
        self.join(timeout=5)
        os.close(self.fd)


def read_record(path: Path):
    raw = path.read_bytes()
    assert len(raw) == PAGE, f"delay-read page must stay {PAGE} bytes, got {len(raw)}"
    magic, seq, at, delay, reads, fallbacks, reason, _ = struct.unpack("<IIQqIIII", raw[:40])
    assert magic == MAGIC_READ and seq % 2 == 0
    return {"reads": reads, "fallbacks": fallbacks, "reason": reason, "delay": delay}


def real_source_trees():
    """Pristine Shairport 5.6.2 sources, if this host has them (CI does not)."""
    candidates = [os.environ.get("LE_SHAIRPORT_SOURCE", ""), "/tmp/sp562x/shairport-sync-5.6.2",
                  "/tmp/bs/shairport-sync-5.6.2"]
    for candidate in candidates:
        root = Path(candidate) if candidate else None
        if not root or not root.is_dir():
            continue
        try:
            if all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
                   for name, digest in UPSTREAM_SHA256.items()):
                return root
        except OSError:
            continue
    return None


def scenario_real_sources(directory: Path):
    root = real_source_trees()
    if root is None:
        print("real Shairport 5.6.2 sources: SKIPPED (no pristine tree found; set "
              "LE_SHAIRPORT_SOURCE)")
        return False
    for order in ("build order (hooks, then pipe-delay)", "pipe-delay alone"):
        tree = directory / ("real-" + order.split()[0])
        tree.mkdir()
        for name in UPSTREAM:
            (tree / name).write_bytes((root / name).read_bytes())
        patches = [HOOK_PATCH, PIPE_PATCH] if order.startswith("build") else [PIPE_PATCH]
        for patch in patches:
            done = subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(patch)],
                                  cwd=tree, timeout=30, capture_output=True, text=True)
            assert done.returncode == 0, done.stdout + done.stderr
            assert "fuzz" not in done.stdout.lower() and "FAILED" not in done.stdout, done.stdout
        assert "le_ad_client_flush(&le_delay)" in (tree / "audio_pipe.c").read_text()
        assert (tree / "player.c").read_text().count("__atomic_fetch_add(&le_ad_ctr_") == 4
    print(f"patches apply to the real 5.6.2 sources ({root}) with fuzz 0: PASS")
    return True


def scenario_flush(binary: Path, directory: Path):
    """flush(): bridge handshake, mutual exclusion with play(), delay() after it."""
    work = directory / "flush"
    work.mkdir()
    pipe = work / "airplay.pcm"
    counters = work / "airplay.pcm.counters"
    os.mkfifo(pipe)
    reader = os.open(pipe, os.O_RDONLY | os.O_NONBLOCK)
    driver = Driver(binary, pipe)
    bridge = None
    try:
        assert driver.ask("prepare") == "prepare rc=0"

        # A. No bridge page: nobody to tell, so flush() returns at once, and an
        #    absent bridge is not a timeout.
        started = time.monotonic()
        assert driver.ask("flush").startswith("flush ms=")
        assert time.monotonic() - started < 0.2
        info = read_counters(counters)
        assert (info["flush_calls"], info["flush_req"], info["flush_timeouts"]) == (1, 1, 0), info
        assert info["at"] > 0

        # B. A live bridge that never answers: flush() gives up after its bounded
        #    wait (never hangs) and counts the timeout.
        bridge = FakeBridge(work, reader, ack=False)
        bridge.start()
        started = time.monotonic()
        driver.ask("flush")
        waited = time.monotonic() - started
        assert FLUSH_WAIT_S - 0.05 <= waited <= FLUSH_WAIT_S + 0.5, waited
        info = read_counters(counters)
        assert (info["flush_calls"], info["flush_req"], info["flush_timeouts"]) == (2, 2, 1), info

        # B2. Mutual exclusion: while that wait holds the I/O mutex, a play() from
        #     another thread waits for it and then completes (bounded, no deadlock).
        text = driver.ask("exclusion")
        play_ms = int(text.split("play_ms=")[1])
        assert 150 <= play_ms <= 900, text
        assert len(os.read(reader, 65536)) == 40  # the 10 frames arrived, whole

        # C. An answering bridge: flush() returns as soon as flush_done matches,
        #    the pipe is emptied and delay() reports the (near-zero) new queue.
        bridge.ack = True
        end = time.monotonic() + 2  # the bridge first answers the request it ignored
        while bridge.done != read_counters(counters)["flush_req"] and time.monotonic() < end:
            time.sleep(0.005)
        assert bridge.done == read_counters(counters)["flush_req"]
        assert driver.ask("play 1000 0") == "play rc=0"
        bridge.downstream = 5000
        time.sleep(0.05)
        got = driver.delay()
        assert got == (0, 6000), got
        started = time.monotonic()
        driver.ask("flush")
        assert time.monotonic() - started < 0.15
        assert bridge.drained >= 4000 and bridge.done == read_counters(counters)["flush_req"]
        assert driver.delay() == (0, 0), "delay() after flush must report the emptied queue"
        info = read_counters(counters)
        assert info["flush_calls"] == 4 and info["flush_timeouts"] == 2, info  # exclusion flushed once
        # New audio after the flush is exactly what the pipe holds, nothing stale.
        assert driver.ask("play 200 100000") == "play rc=0"
        assert driver.delay() == (0, 200)  # just the new audio, nothing stale behind it
        data = os.read(reader, 65536)
        assert data == struct.pack("<200I", *range(100000, 100200)), len(data)

        # D. Counters reach the page (no flush needed) and the page stays 64 bytes.
        assert driver.ask("setctr 3 5 70 20") == "setctr ok"
        driver.delay()
        info = read_counters(counters)
        assert (info["resyncs"], info["late_drops"], info["frames_inserted"],
                info["frames_dropped"]) == (3, 5, 70, 20), info
        assert counters.stat().st_size == PAGE

        # E. Pause/skip while play() is blocked on a full pipe: flush() gets in
        #    between chunks (the writer waits without the mutex) and the stale
        #    remainder of that write is dropped, not written after the flush.
        bridge.ack = False
        assert driver.ask("bgplay 60000 0") == "bgplay started=1"  # 240 KB into a 64 KB pipe
        time.sleep(0.4)
        full = struct.unpack("i", __import__("fcntl").ioctl(
            reader, __import__("termios").FIONREAD, b"\0\0\0\0"))[0]
        assert full >= 60000, full  # blocked: the pipe is full
        bridge.ack = True
        started = time.monotonic()
        driver.ask("flush")
        assert time.monotonic() - started < 0.5, "flush() was held off by a blocked play()"
        assert driver.ask("bgwait", timeout=5) == "bgwait rc=0"
        time.sleep(0.1)
        left = struct.unpack("i", __import__("fcntl").ioctl(
            reader, __import__("termios").FIONREAD, b"\0\0\0\0"))[0]
        assert left == 0, f"stale audio written after the flush: {left} bytes"
        assert driver.ask("play 10 7") == "play rc=0"
        assert os.read(reader, 4096) == struct.pack("<10I", *range(7, 17))

        # F. Two threads hammer play() and flush(): no deadlock, bounded time,
        #    and the pipe only ever carries whole frames.
        total = [0]
        stop = threading.Event()

        def drain():
            while not stop.is_set():
                try:
                    chunk = os.read(reader, 65536)
                    total[0] += len(chunk)
                except BlockingIOError:
                    time.sleep(0.0005)
        thread = threading.Thread(target=drain, daemon=True)
        thread.start()
        before = read_counters(counters)["flush_calls"]
        text = driver.ask("race 300", timeout=30)
        stop.set()
        thread.join(timeout=5)
        elapsed = int(text.split("ms=")[1])
        assert elapsed < 20000, text
        assert total[0] % 4 == 0, total[0]
        assert read_counters(counters)["flush_calls"] == before + 300
    finally:
        if bridge:
            bridge.close()
        driver.close()
        os.close(reader)
    print("Shairport flush(): bridge handshake, play/flush exclusion, delay() after flush: PASS")


def run():
    # 1. Both pinned patches apply with fuzz 0 and expose delay().
    with tempfile.TemporaryDirectory(prefix="le-pipe-patch-") as temp:
        root = Path(temp)
        both = root / "both"
        both.mkdir()
        apply_patches(both)
        patched = (both / "audio_pipe.c").read_text()
        assert ".delay = &delay," in patched and ".delay = NULL," not in patched
        assert ".flush = &flush," in patched and ".flush = NULL," not in patched
        assert "le_ad_client_flush(&le_delay)" in patched
        assert "le_ad_client_write(&le_delay, fd," in patched
        assert "write(fd, buf, samples * bytes_per_frame)" not in patched, "unguarded write left"
        player = (both / "player.c").read_text()
        for site in ("__atomic_fetch_add(&le_ad_ctr_resyncs, 1",
                     "__atomic_fetch_add(&le_ad_ctr_late_drops, 1",
                     "__atomic_fetch_add(&le_ad_ctr_frames_inserted,",
                     "__atomic_fetch_add(&le_ad_ctr_frames_dropped,"):
            assert player.count(site) == 1, site
        assert player.count("uint32_t le_ad_ctr_resyncs, le_ad_ctr_late_drops,") == 1
        # The counter sites sit where Shairport logs (only at debug level 2) the event.
        assert player.index("le_ad_ctr_late_drops, 1") < player.index("drop late packet")
        assert player.index("request_resync = 1;") < player.index("le_ad_ctr_resyncs, 1")
        assert "le_ad_client_delay(&le_delay, fd, bytes_per_frame, the_delay)" in patched
        assert "config.audio_backend_buffer_desired_length = 0.35;" in patched
        assert '#include "airplay_delay.h"' in patched
        import hashlib
        pinned = hashlib.sha256((FIXTURE / "audio_pipe.c").read_bytes()).hexdigest()
        assert pinned == PIPE_SHA256, "fixture audio_pipe.c drifted from the pinned upstream"
        binary = build_driver(root / "build")
        work = root / "run"
        work.mkdir()
        pipe = work / "airplay.pcm"
        bridge = work / "airplay.pcm.delay"
        record = work / "airplay.pcm.delay-read"
        os.mkfifo(pipe)
        reader = os.open(pipe, os.O_RDONLY | os.O_NONBLOCK)
        driver = Driver(binary, pipe)
        try:
            assert driver.banner == "desired=0.350 has_delay=1 has_flush=1", driver.banner
            # Not primed yet: no open pipe means no delay.
            rc, _ = driver.delay()
            assert rc == -19, rc  # -ENODEV, Shairport's "not yet available"
            assert driver.ask("prepare") == "prepare rc=0"

            # 2. No bridge page: fall back (and say why).
            rc, _ = driver.delay()
            assert rc == -19
            assert read_record(record)["reason"] == NO_PAGE

            # 3. Fresh snapshot, nothing draining: pipe frames + downstream, exact.
            bridge_page(bridge, downstream=5000, playing=0)
            assert driver.ask("play 1000 0") == "play rc=0"
            rc, value = driver.delay()
            assert (rc, value) == (0, 6000), (rc, value)
            info = read_record(record)
            assert info["reads"] == 1 and info["delay"] == 6000

            # 4. Draining: the snapshot decays at 48 kHz with its age.
            before = time.monotonic_ns()
            bridge_page(bridge, downstream=5000, playing=1, at=before - 100_000_000)
            rc, value = driver.delay()
            after = time.monotonic_ns()
            assert rc == 0
            low = 1000 + max(0, 5000 - (after - before + 100_000_000) * 48 // 1_000_000)
            high = 1000 + max(0, 5000 - (0 + 100_000_000) * 48 // 1_000_000)
            assert low <= value <= high, (low, value, high)
            assert 1000 + 150 <= value <= 1000 + 330, value  # ~100 ms of 5000 gone

            # 5. Reading the pipe lowers the figure by exactly what left it.
            os.read(reader, 400 * 4)
            bridge_page(bridge, downstream=5000, playing=0)
            assert driver.delay() == (0, 5600)

            # 5b. Output starting: nothing drains, but the start wait (48000
            # of the 52000 frames) runs down with the snapshot's age.
            before = time.monotonic_ns()
            bridge_page(bridge, downstream=52000, playing=0, start=48000,
                        at=before - 100_000_000)
            rc, value = driver.delay()
            after = time.monotonic_ns()
            assert rc == 0
            high = 600 + 52000 - 100_000_000 * 48 // 1_000_000
            low = 600 + 52000 - (after - before + 100_000_000) * 48 // 1_000_000
            assert low <= value <= high, (low, value, high)
            bridge_page(bridge, downstream=4000 + 4800, playing=0, start=4800,
                        at=time.monotonic_ns() - 200_000_000)  # wait over: queue stays
            assert driver.delay() == (0, 600 + 4000), "queued frames never run down"

            # 6. Stale snapshot: refuse rather than guess.
            bridge_page(bridge, downstream=5000, playing=1,
                        at=time.monotonic_ns() - 400_000_000)
            rc, _ = driver.delay()
            assert rc == -19 and read_record(record)["reason"] == STALE

            # 7. Bridge itself cannot vouch for the number.
            bridge_page(bridge, downstream=0, valid=0, reason=ENGINE_STALE)
            rc, _ = driver.delay()
            assert rc == -19 and read_record(record)["reason"] == ENGINE_STALE
            bridge_page(bridge, downstream=0, valid=0, reason=0)
            rc, _ = driver.delay()
            assert rc == -19 and read_record(record)["reason"] == NO_ENGINE

            # 8. A writer stuck mid-update (odd sequence) is never trusted.
            bridge_page(bridge, downstream=5000, seq=3)
            rc, _ = driver.delay()
            assert rc == -19 and read_record(record)["reason"] == RACE

            # 9. Wrong magic.
            bridge_page(bridge, downstream=5000, magic=0x12345678)
            rc, _ = driver.delay()
            assert rc == -19 and read_record(record)["reason"] == NO_PAGE

            # 10. Recovery: the next good snapshot works again, counters kept.
            bridge_page(bridge, downstream=700, playing=0)
            assert driver.delay() == (0, 700 + 600)
            info = read_record(record)
            assert info["reads"] == 6 and info["fallbacks"] == 7, info
            assert record.stat().st_size == PAGE
        finally:
            driver.close()
            os.close(reader)
        print("Shairport pipe-delay patch applies and delay() is exact/safe: PASS")
        scenario_flush(binary, root)
        scenario_real_sources(root)


if __name__ == "__main__":
    run()

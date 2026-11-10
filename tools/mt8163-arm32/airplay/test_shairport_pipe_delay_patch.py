#!/usr/bin/env python3
"""The pipe-delay Shairport patch applies cleanly and exposes a working delay().

Applies shairport-5.6.2-pipe-delay.patch to the pinned upstream audio_pipe.c,
compiles the patched backend against minimal stand-ins for Shairport's
audio.h/common.h and drives its real init/prepare/play/delay entry points
against hand-built bridge pages: fresh, draining, stale, invalid, torn and
missing.  A failing delay() must be -ENODEV so Shairport falls back to its
no-delay() behaviour.
"""
import os
import struct
import subprocess
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures/shairport-5.6.2"
HOOK_PATCH = HERE / "shairport-5.6.2-session-hooks.patch"
PIPE_PATCH = HERE / "shairport-5.6.2-pipe-delay.patch"
UPSTREAM = ("common.c", "common.h", "player.c", "player.h", "audio_pipe.c")
PIPE_SHA256 = "27d5ce1a40923c559b2dcc058b5fc74c24b56d25c1babb0b2644c9c461ad4fa1"

MAGIC_BRIDGE = 0x3142454C
MAGIC_READ = 0x3152454C
PAGE = 64
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
#include <stdio.h>
#include <string.h>
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

int main(int argc, char **argv)
{
  char line[160];
  char *args[1];
  if (argc != 2) return 2;
  args[0] = argv[1];
  config.audio_backend_buffer_desired_length = -1;
  if (audio_pipe.init(1, args) != 0) return 3;
  printf("desired=%.3f has_delay=%d\n", config.audio_backend_buffer_desired_length,
         audio_pipe.delay != NULL);
  /* S16_LE, 2 channels: 4 bytes per frame, as in airplay2.conf. */
  if (audio_pipe.configure((int32_t)(SPS_FORMAT_S16_LE | (2 << 16)), NULL) != 0) return 4;
  fflush(stdout);
  while (fgets(line, sizeof(line), stdin)) {
    unsigned int n = 0, v = 0;
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

    def ask(self, line: str) -> str:
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()
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
                seq=2, magic=MAGIC_BRIDGE):
    at = time.monotonic_ns() if at is None else at
    raw = struct.pack("<IIQIIIIIIII", magic, seq, at, downstream, playing, valid, reason,
                      0, 0, 0, 0)
    path.write_bytes(raw.ljust(PAGE, b"\0"))


def read_record(path: Path):
    raw = path.read_bytes()
    assert len(raw) == PAGE, f"delay-read page must stay {PAGE} bytes, got {len(raw)}"
    magic, seq, at, delay, reads, fallbacks, reason, _ = struct.unpack("<IIQqIIII", raw[:40])
    assert magic == MAGIC_READ and seq % 2 == 0
    return {"reads": reads, "fallbacks": fallbacks, "reason": reason, "delay": delay}


def run():
    # 1. Both pinned patches apply with fuzz 0 and expose delay().
    with tempfile.TemporaryDirectory(prefix="le-pipe-patch-") as temp:
        root = Path(temp)
        both = root / "both"
        both.mkdir()
        apply_patches(both)
        patched = (both / "audio_pipe.c").read_text()
        assert ".delay = &delay," in patched and ".delay = NULL," not in patched
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
            assert driver.banner == "desired=0.350 has_delay=1", driver.banner
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
            assert info["reads"] == 4 and info["fallbacks"] == 7, info
            assert record.stat().st_size == PAGE
        finally:
            driver.close()
            os.close(reader)
    print("Shairport pipe-delay patch applies and delay() is exact/safe: PASS")


if __name__ == "__main__":
    run()

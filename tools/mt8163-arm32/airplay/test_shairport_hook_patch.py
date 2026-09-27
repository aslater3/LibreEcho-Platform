#!/usr/bin/env python3
"""Apply the pinned Shairport patch and exercise its compiled hook arguments."""
from pathlib import Path
import os
import subprocess
import tempfile
from test_airplay_generation_fence import compile_fixture, wait_for

HERE = Path(__file__).resolve().parent
PATCH = HERE / "shairport-5.1-session-hooks.patch"
UPSTREAM = ("common.c", "common.h", "player.c", "player.h")

DRIVER = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <fcntl.h>
static struct {
    const char *cmd_set_volume, *cmd_start, *cmd_stop;
    int cmd_blocking, cmd_start_returns_output;
} config;
#define debug(...) ((void)0)
#define warn(...) ((void)0)
#define inform(...) ((void)0)
static void safe_socket_close(int *fd) { close(*fd); }
static int poptParseArgvString(const char *text, int *argc, const char ***argv)
{
    char *copy = strdup(text), *part;
    const char **args = calloc(8, sizeof(*args));
    assert(copy && args);
    *argc = 0;
    for (part = strtok(copy, " "); part; part = strtok(NULL, " ")) {
        assert(*argc < 7);
        args[(*argc)++] = strdup(part);
    }
    *argv = args;
    free(copy);
    return 0;
}
'''

DRIVER_MAIN = r'''
typedef struct {
    char playback_session_token[33];
    int own_airplay_volume_set;
    double own_airplay_volume;
} rtsp_conn_info;
static void player_volume_without_notification(double volume, rtsp_conn_info *conn)
{
    conn->own_airplay_volume = volume;
}
/* The preceding function is copied verbatim from the patched player.c. */
int main(int argc, char **argv)
{
    rtsp_conn_info a = {0}, b = {0};
    char start[512], stop[512], volume[512], path[512], text[128];
    FILE *f;
    assert(argc == 3);
    assert(snprintf(start, sizeof(start), "%s --start", argv[1]) < (int)sizeof(start));
    assert(snprintf(stop, sizeof(stop), "%s --stop", argv[1]) < (int)sizeof(stop));
    assert(snprintf(volume, sizeof(volume), "%s --set-volume", argv[1]) < (int)sizeof(volume));
    config.cmd_start = start; config.cmd_stop = stop; config.cmd_set_volume = volume;
    config.cmd_blocking = 1;
    /* Pre-start SET_PARAMETER is saved but cannot invoke an untagged hook. */
    player_volume(-20, &a);
    assert(a.own_airplay_volume_set && a.own_airplay_volume == -20);
    assert(new_playback_session_token(a.playback_session_token) == 0);
    assert(new_playback_session_token(b.playback_session_token) == 0);
    assert(strcmp(a.playback_session_token, b.playback_session_token));
    command_start(a.playback_session_token);
    player_volume(a.own_airplay_volume, &a);
    command_stop(a.playback_session_token);
    command_start(b.playback_session_token);
    command_set_volume(-6, a.playback_session_token); /* late A child */
    command_stop(a.playback_session_token);           /* late A stop */
    assert(snprintf(path, sizeof(path), "%s/airplay.active", argv[2]) < (int)sizeof(path));
    f = fopen(path, "r"); assert(f && fgets(text, sizeof(text), f)); fclose(f);
    assert(strlen(text) == 33 && !strncmp(text, b.playback_session_token, 32));
    assert(snprintf(path, sizeof(path), "%s/airplay.volume", argv[2]) < (int)sizeof(path));
    assert(access(path, F_OK) != 0);
    player_volume(-12, &b);
    f = fopen(path, "r"); assert(f && fgets(text, sizeof(text), f)); fclose(f);
    assert(!strcmp(text, "-12.000000\n"));
    command_stop(b.playback_session_token);
    assert(access(path, F_OK) != 0);
    return 0;
}
'''


def compiled_protocol(root):
    common = (root / "common.c").read_text()
    player = (root / "player.c").read_text()
    hooks = common[common.index("void command_set_volume("):common.index("// this is for reading an unsigned 32 bit number")]
    nonce = player[player.index("static int new_playback_session_token("):player.index("int player_play(")]
    volume = player[player.index("void player_volume(double airplay_volume,"):player.index("void do_flush(")]
    # Definitions precede the exact player function under test.
    src = DRIVER + hooks + nonce + DRIVER_MAIN.replace("/* The preceding function is copied verbatim from the patched player.c. */", volume)
    (root / "protocol.c").write_text(src)
    subprocess.run([os.environ.get("CC", "cc"), "-std=c99", "-Wall", "-Wextra", "-Werror",
                    str(root / "protocol.c"), "-o", str(root / "protocol")], check=True, timeout=60)
    compile_fixture(root / "bridge-fixture")
    engine = subprocess.Popen([str(root / "bridge-fixture/engine"),
                               str(root / "bridge-fixture"), str(root / "bridge-fixture/observed")])
    try:
        wait_for(lambda: (root / "bridge-fixture/airplay-media.pcm").exists())
        subprocess.run([str(root / "protocol"), str(root / "bridge-fixture/bridge"),
                        str(root / "bridge-fixture")], check=True, timeout=20)
    finally:
        engine.terminate()
        engine.wait(timeout=3)


def run():
    assert PATCH.is_file(), "missing pinned Shairport hook patch"
    with tempfile.TemporaryDirectory(prefix="le-shairport-patch-") as directory:
        root = Path(directory)
        fixture = HERE / "fixtures/shairport-5.1"
        for name in UPSTREAM:
            (root / name).write_bytes((fixture / name).read_bytes())
        subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(PATCH)],
                       cwd=root, check=True, timeout=20)
        common = (root / "common.c").read_text()
        player = (root / "player.c").read_text()
        assert "command_start(conn->playback_session_token)" in player
        assert "command_stop(conn->playback_session_token)" in player
        assert "command_set_volume(airplay_volume, conn->playback_session_token)" in player
        assert '"%s %s %f"' in common
        assert "if (conn->playback_session_token[0])" in player
        (root / "bridge-fixture").mkdir()
        compiled_protocol(root)
        # The full pinned sources, when supplied, must also apply exactly.
        source = os.environ.get("SHAIRPORT_SOURCE")
        if source:
            full = root / "full"
            full.mkdir()
            for name in UPSTREAM:
                (full / name).write_bytes((Path(source) / name).read_bytes())
            subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(PATCH)],
                           cwd=full, check=True, timeout=20)
    print("Shairport pinned hook patch / protocol fixture: PASS")


if __name__ == "__main__":
    run()

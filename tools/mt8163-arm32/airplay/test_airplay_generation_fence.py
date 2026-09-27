#!/usr/bin/env python3
"""Compiled bridge/engine FIFO generation-fence integration regression."""
import os
from pathlib import Path
import select
import signal
import subprocess
import fcntl
import tempfile
import time

from test_airplay_session_dsp import ENGINE_TEST, HERE
from test_audio_period_buffer import MIXER_HEADER, PCM_HEADER

ENGINE_DRIVER = ENGINE_TEST.split("static void put(")[0] + r'''
int main(int argc, char **argv)
{
    struct source_bus buses[SOURCE_COUNT] = {0};
    FILE *log;
    size_t last_partial = 0;
    assert(argc == 3);
    assert(setup_sources(buses, argv[1]) == 0);
    log = fopen(argv[2], "w"); assert(log);
    setvbuf(log, NULL, _IONBF, 0);
    while (!stopping) {
        assert(poll_sources(buses, 20) >= 0);
        assert(read_sources(buses, argv[1]) >= 0);
        if (buses[SOURCE_AIRPLAY].received &&
            buses[SOURCE_AIRPLAY].received < PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t) &&
            buses[SOURCE_AIRPLAY].received != last_partial)
            fprintf(log, "partial:%zu\n", buses[SOURCE_AIRPLAY].received);
        last_partial = buses[SOURCE_AIRPLAY].received;
        if (source_period_ready(&buses[SOURCE_AIRPLAY])) {
            int16_t value = buses[SOURCE_AIRPLAY].samples[0];
            fprintf(log, "%d\n", value);
            consume_period(buses);
        }
    }
    fclose(log);
    close_sources(buses);
    return 0;
}
'''


def wait_for(predicate, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("timed out waiting for engine/bridge")


def compile_fixture(root, fixed_nonce=False, pause_before_marker=False):
    (root / "tinyalsa").mkdir()
    (root / "tinyalsa/mixer.h").write_text(MIXER_HEADER)
    (root / "tinyalsa/pcm.h").write_text(PCM_HEADER)
    (root / "engine.c").write_text(ENGINE_DRIVER)
    bridge_source = ((HERE / "airplay_audio.c").read_text()
                     .replace("/run/libreecho-audio/", str(root) + "/")
                     .replace("/run/libreecho/airplay.pcm", str(root / "input.pcm")))
    if fixed_nonce:
        (root / "nonce").write_bytes(bytes(16))
        bridge_source = bridge_source.replace('"/dev/urandom"', '"' + str(root / "nonce") + '"')
    if pause_before_marker:
        before = 'if (!result && !strcmp(argv[1], "--start"))\n'
        assert bridge_source.count(before) == 1
        bridge_source = bridge_source.replace(before,
            'if (!result && !strcmp(argv[1], "--start")) raise(SIGSTOP);\n\t\t' + before)
    (root / "bridge.c").write_text(bridge_source)
    assert "input = open(input_path, O_RDWR | O_NONBLOCK | O_CLOEXEC);" in (root / "bridge.c").read_text()
    cc = os.getenv("CC", "cc")
    subprocess.run([cc, "-std=c99", "-Wall", "-Wextra", "-Werror", "-ffunction-sections",
                    "-fdata-sections", "-I", str(root), "-I", str(HERE),
                    str(root / "engine.c"), "-Wl,--gc-sections", "-lm", "-o", str(root / "engine")],
                   check=True, timeout=60)
    subprocess.run([cc, "-std=c99", "-Wall", "-Wextra", "-Werror", str(root / "bridge.c"),
                    "-lm", "-o", str(root / "bridge")], check=True, timeout=60)


def hook(root, arg, timeout=3):
    return subprocess.run([str(root / "bridge"), arg], timeout=timeout).returncode


def admit(root):
    assert subprocess.run([str(root / "bridge"), "--set-volume", "-12"],
                          timeout=3).returncode == 0
    marker = (root / "airplay.active").stat()
    volume = (root / "airplay.volume").stat()
    def fields(stat):
        return (stat.st_dev, stat.st_ino, stat.st_ctime_ns // 1_000_000_000,
                stat.st_ctime_ns % 1_000_000_000)
    (root / "airplay.master").write_text(" ".join(map(str, (*fields(marker), *fields(volume)))) + "\n")


def run():
    with tempfile.TemporaryDirectory(prefix="le-fence-") as temp:
        root = Path(temp)
        compile_fixture(root)
        log = root / "observed"
        engine = subprocess.Popen([str(root / "engine"), str(root), str(log)])
        bridge = None
        try:
            fifo = root / "airplay-media.pcm"
            wait_for(fifo.exists)
            os.mkfifo(root / "input.pcm")
            bridge = subprocess.Popen([str(root / "bridge")])
            inp = os.open(root / "input.pcm", os.O_RDWR | os.O_NONBLOCK)
            try:
                # A stopped/started engine that never observed an absent marker must
                # drain queued periods and live FIFO before publishing B's marker.
                assert hook(root, "--start") == 0
                admit(root)
                a = (1100).to_bytes(2, "little", signed=True) * 4096
                b = (2200).to_bytes(2, "little", signed=True) * 4096
                os.write(inp, a)
                wait_for(lambda: "1100" in log.read_text())
                # Leave an incomplete predecessor period in the engine queue.
                os.write(inp, a[:4096])
                wait_for(lambda: "partial:4096" in log.read_text().splitlines())
                assert hook(root, "--stop") == 0
                # Old Shairport bytes can remain in the bridge's INPUT FIFO,
                # not just the engine FIFO. Freeze the bridge so they survive.
                bridge.send_signal(signal.SIGSTOP)
                os.write(inp, a)
                # Poison the persistent engine FIFO too. Neither may reach B.
                out = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                os.write(out, a)
                os.close(out)
                assert hook(root, "--start") == 0
                boundary = len(log.read_text().splitlines())
                bridge.send_signal(signal.SIGCONT)
                os.write(inp, b)  # marker and first PCM in same engine poll interval
                wait_for(lambda: "2200" in log.read_text().splitlines()[boundary:])
                assert "1100" not in log.read_text().splitlines()[boundary:]
                # Callback attempted while a stop reset owns the lock must
                # wait, then fail against the absent marker (not resurrect A).
                engine.send_signal(signal.SIGSTOP)
                stopping_hook = subprocess.Popen([str(root / "bridge"), "--stop"])
                wait_for(lambda: (root / "airplay.reset").exists())
                callback = subprocess.Popen([str(root / "bridge"), "--set-volume", "-12"])
                try:
                    assert callback.poll() is None
                finally:
                    engine.send_signal(signal.SIGCONT)
                assert stopping_hook.wait(timeout=3) == 0
                assert callback.wait(timeout=3) != 0
                assert not (root / "airplay.volume").exists()
            finally:
                os.close(inp)
        finally:
            for proc in (bridge, engine):
                if proc is not None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill(); proc.wait(timeout=2)

    # Engine restart with a stale active marker must stay unarmed until a new
    # handshake. The first post-reset period must not be lost on restart.
    with tempfile.TemporaryDirectory(prefix="le-fence-restart-") as temp:
        root = Path(temp)
        compile_fixture(root)
        (root / "airplay.active").write_text("")
        log = root / "observed"
        engine = subprocess.Popen([str(root / "engine"), str(root), str(log)])
        try:
            fifo = root / "airplay-media.pcm"
            wait_for(fifo.exists)
            out = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            try:
                stale = (1100).to_bytes(2, "little", signed=True) * 4096
                fresh = (2200).to_bytes(2, "little", signed=True) * 4096
                os.write(out, stale)
                # Polling idle state does not authorize the surviving marker.
                time.sleep(.1)
                assert not log.read_text()
                assert hook(root, "--start") == 0
                os.write(out, fresh)
                wait_for(lambda: "2200" in log.read_text().splitlines())
                assert "1100" not in log.read_text().splitlines()
            finally:
                os.close(out)
        finally:
            engine.terminate(); engine.wait(timeout=2)

    # An engine restarted with a still-matching on-disk ack must re-drain
    # the FIFO before replacing that ack; equality on disk is not process proof.
    with tempfile.TemporaryDirectory(prefix="le-fence-replay-") as temp:
        root = Path(temp)
        compile_fixture(root)
        token = "a" * 32 + "\n"
        (root / "airplay.reset").write_text(token)
        (root / "airplay.reset-ack").write_text(token)
        old_ack = (root / "airplay.reset-ack").stat().st_ino
        os.mkfifo(root / "airplay-media.pcm")
        out = os.open(root / "airplay-media.pcm", os.O_RDWR | os.O_NONBLOCK)
        stale = (1100).to_bytes(2, "little", signed=True) * 4096
        fresh = (2200).to_bytes(2, "little", signed=True) * 4096
        os.write(out, stale)
        log = root / "observed"
        engine = subprocess.Popen([str(root / "engine"), str(root), str(log)])
        try:
            wait_for(lambda: (root / "airplay.reset-ack").stat().st_ino != old_ack)
            (root / "airplay.active").write_text("")
            os.write(out, fresh)
            wait_for(lambda: "2200" in log.read_text().splitlines())
            assert "1100" not in log.read_text().splitlines()
        finally:
            engine.terminate(); engine.wait(timeout=2)
            os.close(out)

    # A held lock must not leave an active marker or callback on --stop failure.
    with tempfile.TemporaryDirectory(prefix="le-fence-lock-") as temp:
        root = Path(temp)
        compile_fixture(root)
        for name in ("airplay.active", "airplay.volume", "airplay.master"):
            (root / name).write_text("stale\n")
        lock = os.open(root / "airplay.lock", os.O_CREAT | os.O_RDWR, 0o640)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            assert hook(root, "--stop") != 0
            for name in ("airplay.active", "airplay.volume", "airplay.master"):
                assert not (root / name).exists(), name
        finally:
            os.close(lock)

    # Restart between the first engine's reset ack and marker publication.
    # A successful hook must not leave the replacement engine permanently
    # disarmed; its new acknowledgement must follow a fresh FIFO drain.
    with tempfile.TemporaryDirectory(prefix="le-fence-handoff-") as temp:
        root = Path(temp)
        compile_fixture(root, pause_before_marker=True)
        log = root / "observed"
        engine = subprocess.Popen([str(root / "engine"), str(root), str(log)])
        hook_process = None
        out = None
        try:
            fifo = root / "airplay-media.pcm"
            wait_for(fifo.exists)
            out = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            hook_process = subprocess.Popen([str(root / "bridge"), "--start"])
            wait_for(lambda: Path(f"/proc/{hook_process.pid}/status").read_text()
                     .split("State:\t", 1)[1].lstrip().startswith("T"))
            old_ack = (root / "airplay.reset-ack").stat().st_ino
            engine.terminate(); engine.wait(timeout=2)
            engine = subprocess.Popen([str(root / "engine"), str(root), str(log)])
            wait_for(lambda: (root / "airplay.reset-ack").stat().st_ino != old_ack)
            hook_process.send_signal(signal.SIGCONT)
            assert hook_process.wait(timeout=3) == 0
            admit(root)
            first = (2200).to_bytes(2, "little", signed=True) * 4096
            os.write(out, first)
            wait_for(lambda: "2200" in log.read_text().splitlines())
        finally:
            if hook_process is not None and hook_process.poll() is None:
                hook_process.send_signal(signal.SIGCONT)
                hook_process.terminate(); hook_process.wait(timeout=2)
            engine.terminate(); engine.wait(timeout=2)
            if out is not None:
                os.close(out)

    # A stale matching acknowledgement cannot authorize a new hook if the
    # engine dies before servicing its freshly published reset request.
    with tempfile.TemporaryDirectory(prefix="le-fence-old-ack-") as temp:
        root = Path(temp)
        compile_fixture(root, fixed_nonce=True)
        (root / "airplay.reset-ack").write_text("0" * 32 + "\n")
        assert hook(root, "--start") != 0
        assert not (root / "airplay.active").exists()

    # No engine ack: fail closed; neither marker nor callback is published.
    with tempfile.TemporaryDirectory(prefix="le-fence-no-engine-") as temp:
        root = Path(temp)
        compile_fixture(root)
        (root / "airplay.reset-ack").write_text("0" * 32 + "\n")
        assert hook(root, "--start") != 0
        assert not (root / "airplay.active").exists()
        assert subprocess.run([str(root / "bridge"), "--set-volume", "-12"], timeout=3).returncode != 0
        assert hook(root, "--stop") != 0
        assert not (root / "airplay.active").exists()
    print("compiled bridge/engine generation fence: PASS")


if __name__ == "__main__":
    run()

#!/usr/bin/env python3
"""Compiled bridge regression: volume hooks must not starve behind PCM forwarding.

A saturated Shairport producer and a paced engine consumer keep the bridge
re-acquiring the session lock for every chunk. Before the waiter gate, each
``--set-volume`` hook lost every nonblocking retry and failed after ~1 s, so
sender volume changes were dropped during playback.
"""
import fcntl
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from test_airplay_generation_fence import A, compile_fixture, wait_for

CHUNK = 8192
PACE = 0.02
F_SETPIPE_SZ = 1031
HOOKS = 8
HOOK_BUDGET = 0.5  # seconds; the bounded lock budget is ~1 s


def run():
    with tempfile.TemporaryDirectory(prefix="le-hook-priority-") as directory:
        root = Path(directory)
        compile_fixture(root)
        sink_path = root / "airplay-media.pcm"
        os.mkfifo(sink_path)
        os.mkfifo(root / "input.pcm")
        sink = os.open(sink_path, os.O_RDWR | os.O_NONBLOCK)
        fcntl.fcntl(sink, F_SETPIPE_SZ, CHUNK)
        stop = threading.Event()
        consumed = [0]

        def consumer():
            last = ""
            while not stop.is_set():
                try:
                    request = (root / "airplay.reset").read_text()
                except FileNotFoundError:
                    request = ""
                if len(request) == 33 and request != last:
                    try:
                        while os.read(sink, CHUNK):
                            pass
                    except BlockingIOError:
                        pass
                    temporary = root / "airplay.reset-ack.tmp"
                    temporary.write_text(request)
                    os.rename(temporary, root / "airplay.reset-ack")
                    last = request
                try:
                    consumed[0] += len(os.read(sink, CHUNK))
                except BlockingIOError:
                    pass
                time.sleep(PACE)

        def producer(fd):
            pcm = b"\x55" * CHUNK
            while not stop.is_set():
                try:
                    os.write(fd, pcm)
                except BlockingIOError:
                    time.sleep(.001)

        threads = [threading.Thread(target=consumer, daemon=True)]
        threads[0].start()
        bridge = subprocess.Popen([str(root / "bridge")])
        inp = os.open(root / "input.pcm", os.O_RDWR | os.O_NONBLOCK)
        try:
            def hook(*args):
                started = time.monotonic()
                rc = subprocess.run([str(root / "bridge"), *args], timeout=5).returncode
                return rc, time.monotonic() - started

            assert hook("--start", A)[0] == 0
            threads.append(threading.Thread(target=producer, args=(inp,), daemon=True))
            threads[1].start()
            wait_for(lambda: consumed[0] >= 4 * CHUNK, timeout=3)
            results = []
            for index in range(HOOKS):
                time.sleep(.075)  # let forwarding resume between callbacks
                db = "-%d" % (10 + index)
                rc, elapsed = hook("--set-volume", A, db)
                results.append((rc, round(elapsed * 1000, 1)))
                assert rc == 0, results
                assert elapsed < HOOK_BUDGET, results
                assert (root / "airplay.volume").read_text() == "%s.000000\n" % db
            before = consumed[0]
            wait_for(lambda: consumed[0] > before + CHUNK, timeout=3)  # forwarding still live

            # A registered waiter must hold off the next chunk, and a held
            # session lock must still produce the bounded failure unchanged.
            waiters = os.open(root / "airplay.waiters", os.O_RDWR)
            try:
                fcntl.flock(waiters, fcntl.LOCK_SH)
                time.sleep(.1)
                try:
                    while os.read(sink, CHUNK):
                        pass
                except BlockingIOError:
                    pass
                paused = consumed[0]
                time.sleep(.2)
                assert consumed[0] == paused, "bridge forwarded past a registered waiter"
            finally:
                os.close(waiters)
            wait_for(lambda: consumed[0] > paused, timeout=3)
            lock = os.open(root / "airplay.lock", os.O_RDWR)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                rc, elapsed = hook("--set-volume", A, "-3")
                assert rc != 0 and elapsed >= .9, (rc, elapsed)
                assert (root / "airplay.volume").read_text() == "-17.000000\n"
            finally:
                os.close(lock)
            print("hook latencies (rc, ms):", results)
        finally:
            stop.set()
            bridge.terminate()
            try:
                bridge.wait(timeout=2)
            except subprocess.TimeoutExpired:
                bridge.kill(); bridge.wait(timeout=2)
            for thread in threads:
                thread.join(timeout=2)
            os.close(inp)
            os.close(sink)


if __name__ == "__main__":
    run()
    print("PASS: AirPlay volume hooks are not starved by PCM forwarding")

#!/usr/bin/env python3
"""Independent compiled hook contract: callback bounds and serialized state."""
import fcntl
import os
from pathlib import Path
import subprocess
import tempfile

from test_airplay_generation_fence import compile_fixture, wait_for

A = "a" * 32
B = "b" * 32


def main():
    with tempfile.TemporaryDirectory(prefix="le-airplay-hooks-") as directory:
        root = Path(directory)
        compile_fixture(root)
        engine = subprocess.Popen([str(root / "engine"), str(root), str(root / "observed")])
        try:
            wait_for(lambda: (root / "airplay-media.pcm").exists())
            binary = root / "bridge"

            def hook(*args):
                return subprocess.run([str(binary), *args], timeout=4).returncode

            marker = root / "airplay.active"
            volume = root / "airplay.volume"
            ack = root / "airplay.master"
            generic = root / "media.volume"
            generic.write_text("-6\n")
            assert hook("--set-volume", A, "-12") != 0 and not volume.exists()
            assert hook("--start", A) == 0 and marker.read_text() == A + "\n"
            assert not volume.exists()
            for invalid in ("nan", "inf", "1", "-145", "-31", "-0.5junk"):
                assert hook("--set-volume", A, invalid) != 0 and not volume.exists(), invalid
            assert hook("--set-volume", A, "-30") == 0 and volume.read_text() == "-30.000000\n"
            assert hook("--set-volume", A, "-144") == 0 and volume.read_text() == "-144.000000\n"
            assert generic.read_text() == "-6\n"
            ack.write_text("stale")
            assert hook("--stop", A) == 0 and not marker.exists() and not volume.exists() and not ack.exists()
            assert hook("--set-volume", A, "-12") != 0 and not volume.exists()
            assert hook("--start", B) == 0 and not volume.exists() and not ack.exists()
            lock_fd = os.open(root / "airplay.lock", os.O_CREAT | os.O_RDWR, 0o640)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                child = subprocess.Popen([str(binary), "--set-volume", B, "-12"])
                try:
                    try:
                        child.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        pass
                    else:
                        raise AssertionError("callback escaped the session lock")
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    assert child.wait(timeout=3) == 0
                assert volume.read_text() == "-12.000000\n"
            finally:
                os.close(lock_fd)
            assert hook("--stop", B) == 0 and generic.read_text() == "-6\n"
            print("AirPlay hook callback bounds / serialized session state: PASS")
        finally:
            engine.terminate()
            try:
                engine.wait(timeout=2)
            except subprocess.TimeoutExpired:
                engine.kill(); engine.wait(timeout=2)


if __name__ == "__main__":
    main()

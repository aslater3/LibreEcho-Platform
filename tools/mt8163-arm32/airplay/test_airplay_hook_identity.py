#!/usr/bin/env python3
"""Compiled bridge regression: delayed hook from A cannot alter playback B."""
from pathlib import Path
import subprocess
import tempfile

from test_airplay_generation_fence import compile_fixture, wait_for

A = "a" * 32
B = "b" * 32


def run():
    with tempfile.TemporaryDirectory(prefix="le-hook-identity-") as directory:
        root = Path(directory)
        compile_fixture(root)
        engine = subprocess.Popen([str(root / "engine"), str(root), str(root / "observed")])
        try:
            wait_for(lambda: (root / "airplay-media.pcm").exists())
            binary = root / "bridge"
            marker = root / "airplay.active"
            volume = root / "airplay.volume"

            def hook(*args):
                return subprocess.run([str(binary), *args], timeout=4).returncode

            # A delayed legacy, untagged callback would overwrite B today.
            marker.write_text(B + "\n")
            assert hook("--set-volume", "-6") != 0
            assert not volume.exists() and marker.read_text() == B + "\n"
            marker.unlink()
            assert hook("--start", A) == 0
            assert marker.read_text() == A + "\n"
            assert hook("--set-volume", A, "-20") == 0
            assert volume.read_text() == "-20.000000\n"
            assert hook("--stop", A) == 0
            assert hook("--start", B) == 0
            assert marker.read_text() == B + "\n"
            # Callback forked by A before B's start, scheduled only afterwards.
            assert hook("--set-volume", A, "-6") != 0
            assert not volume.exists()
            assert hook("--stop", A) != 0
            assert marker.read_text() == B + "\n"
            assert hook("--set-volume", B, "-12") == 0
            assert volume.read_text() == "-12.000000\n"
            for invalid_marker in (B, B + "\nextra", A + "\n", "invalid\n"):
                marker.write_text(invalid_marker)
                assert hook("--set-volume", B, "-3") != 0
                assert hook("--stop", B) != 0
                assert marker.read_text() == invalid_marker
                assert volume.read_text() == "-12.000000\n"
            marker.unlink()
            marker.symlink_to(volume)
            assert hook("--set-volume", B, "-3") != 0
            assert hook("--stop", B) != 0
            marker.unlink()
            marker.write_text(B + "\n")
            for bad in ("", "a" * 31, "A" * 32, "g" * 32, "a" * 33, "a" * 32 + "\n"):
                assert hook("--set-volume", bad, "-3") != 0
                assert hook("--stop", bad) != 0
                assert volume.read_text() == "-12.000000\n"
                assert marker.read_text() == B + "\n"
            assert hook("--set-volume", "-3") != 0  # old untagged form
            assert hook("--stop") != 0
            assert hook("--start") != 0
            assert marker.read_text() == B + "\n"
            assert hook("--stop", B) == 0
            assert not marker.exists() and not volume.exists()
        finally:
            engine.terminate()
            try:
                engine.wait(timeout=2)
            except subprocess.TimeoutExpired:
                engine.kill(); engine.wait(timeout=2)
    print("compiled AirPlay hook identity / late callback: PASS")


if __name__ == "__main__":
    run()

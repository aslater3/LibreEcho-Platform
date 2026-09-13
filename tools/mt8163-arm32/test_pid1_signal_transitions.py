#!/usr/bin/env python3
"""Exercise libreecho-init's PID 1 signal transitions without touching a device."""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


SIGNALS = (
    ("TERM", signal.SIGTERM, "reboot"),
    ("INT", signal.SIGINT, "reboot"),
    ("USR1", signal.SIGUSR1, "halt"),
    ("USR2", signal.SIGUSR2, "poweroff"),
)


def source_fragments(init_path: Path) -> tuple[str, str, str]:
    source = init_path.read_text()

    function_start = source.index("signal_transition()\n")
    function_end = source.index(
        "\n}\ntrap 'signal_transition reboot'   TERM", function_start
    ) + 3
    function = source[function_start:function_end]

    trap_start = source.index("trap 'signal_transition reboot'   TERM", function_end)
    trap_end = source.index("\nlog reboot-signal-traps-installed", trap_start)
    traps = source[trap_start:trap_end]

    idle_start = source.index(
        "while true; do\n", source.index("# Android init remains PID 1")
    )
    idle_end = source.index("\ndone", idle_start) + len("\ndone")
    idle = source[idle_start:idle_end]

    expected_traps = [
        "trap 'signal_transition reboot'   TERM",
        "trap 'signal_transition reboot'   INT",
        "trap 'signal_transition halt'     USR1",
        "trap 'signal_transition poweroff' USR2",
    ]
    for expected in expected_traps:
        if expected not in traps:
            raise AssertionError(f"missing live init trap: {expected}")
    if "$BB sleep 3600 &\n    wait $!" not in idle:
        raise AssertionError("live init idle loop is not interruptible")
    return function, traps, idle


def run_shell(
    shell_command: list[str], shell_name: str, fragments: tuple[str, str, str]
) -> None:
    function, traps, idle = fragments
    with tempfile.TemporaryDirectory(prefix="libreecho-signal-test-") as temporary:
        root = Path(temporary)
        fake_busybox = root / "fake-busybox"
        fake_busybox.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "printf '%s\\n' \"$*\" >> \"$LIBREECHO_FAKE_BB_LOG\"\n"
            "case \"${1:-}\" in\n"
            "    sleep) exec sleep \"${2:-0}\" ;;\n"
            "    sync|reboot|halt|poweroff) exit 0 ;;\n"
            "    *) exit 0 ;;\n"
            "esac\n"
        )
        fake_busybox.chmod(0o755)
        fixture = root / "signal-fixture.sh"
        event_log = root / "events.log"
        command_log = root / "busybox.log"
        fixture.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            f"BB={shlex_quote(str(fake_busybox))}\n"
            f"EVENT_LOG={shlex_quote(str(event_log))}\n"
            "log() { printf '%s\\n' \"$*\" >> \"$EVENT_LOG\"; }\n"
            f"{function}\n"
            f"{traps}\n"
            "printf 'READY\\n' >> \"$EVENT_LOG\"\n"
            f"{idle}\n"
        )
        fixture.chmod(0o755)

        environment = os.environ.copy()
        environment["LIBREECHO_FAKE_BB_LOG"] = str(command_log)
        for signal_name, signum, transition in SIGNALS:
            event_log.unlink(missing_ok=True)
            command_log.unlink(missing_ok=True)
            process = subprocess.Popen(
                shell_command + [str(fixture)],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
                text=True,
            )
            try:
                wait_for_line(event_log, "READY", process, shell_name)
                started = time.monotonic()
                os.kill(process.pid, signum)
                expected_command = f"{transition} -f"
                wait_for_line(command_log, expected_command, process, shell_name)
                elapsed = time.monotonic() - started
                if elapsed >= 2.0:
                    raise AssertionError(
                        f"{shell_name} {signal_name} transition took {elapsed:.2f}s"
                    )
                print(
                    f"PASS  {shell_name} {signal_name} -> {expected_command} "
                    f"({elapsed:.2f}s)"
                )
            finally:
                kill_process_group(process)
                process.wait()


def wait_for_line(
    path: Path, expected: str, process: subprocess.Popen[str], shell_name: str
) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if path.is_file() and expected in path.read_text().splitlines():
            return
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise AssertionError(
                f"{shell_name} exited before {expected!r}: {stderr}"
            )
        time.sleep(0.01)
    contents = path.read_text() if path.is_file() else "<no log>"
    raise AssertionError(f"{shell_name} did not emit {expected!r}: {contents}")


def kill_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def main() -> int:
    init_path = Path(__file__).with_name("initramfs") / "libreecho-init"
    fragments = source_fragments(init_path)
    configured_shell = os.environ.get("LIBREECHO_SIGNAL_TEST_SHELL")
    shells = (
        [([configured_shell], configured_shell)]
        if configured_shell
        else [(["/bin/sh"], "/bin/sh")]
    )
    busybox = shutil.which("busybox")
    if busybox and not configured_shell:
        shells.append(([busybox, "sh"], f"{busybox} sh"))
    for command, name in shells:
        run_shell(command, name, fragments)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, subprocess.SubprocessError) as error:
        print(f"FAIL  {error}", file=sys.stderr)
        raise SystemExit(1)

"""Host tests for le-hwsim (Platform #62). Never touches a device.

Protocol conformance always runs. The golden test runs the real
libreecho-web --backend linux (an x86 host build of LibreEcho-UI, path in
LE_HWSIM_WEB_BIN) against the simulator inside a bwrap user namespace whose
/run/libreecho is the simulator's socket directory; it is skipped when either
is unavailable.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from le_hwsim.protocol import MSG_MAX  # noqa: E402
from le_hwsim.simulator import Simulator, run_scenario  # noqa: E402


def call(path: Path, line: bytes) -> dict:
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(5)
        s.connect(str(path))
        s.sendall(line)
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(8192)
            if not chunk:
                break
            data += chunk
    return json.loads(data)


def cmd(path: Path, command: str, args=None, request_id=7) -> dict:
    return call(path, (json.dumps({"v": 1, "id": request_id, "cmd": command, "args": args or {}}) + "\n").encode())


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.sim = Simulator(self.dir, self.dir / "events.jsonl")

    def tearDown(self):
        self.sim.close()
        self.tmp.cleanup()

    def test_every_adapter_answers_status_and_echoes_id(self):
        for sock in ("audio.sock", "led.sock", "network.sock", "bluetooth.sock"):
            reply = cmd(self.dir / sock, "status", request_id=41)
            self.assertEqual((reply["v"], reply["id"], reply["ok"]), (1, 41, True), sock)
            self.assertIsInstance(reply["data"], dict)

    def test_errors_are_ok_false_never_a_dropped_connection(self):
        audio = self.dir / "audio.sock"
        self.assertFalse(cmd(audio, "no_such_command")["ok"])
        self.assertFalse(call(audio, b"{not json\n")["ok"])
        self.assertFalse(call(audio, b'{"v":2,"id":1,"cmd":"status"}\n')["ok"])
        self.assertFalse(call(audio, b'{"v":1,"id":1,"cmd":"status","args":[]}\n')["ok"])
        self.assertFalse(cmd(audio, "set_volume", {"volume": 101})["ok"])
        self.assertFalse(cmd(audio, "set_volume", {"volume": "50"})["ok"])
        big = b'{"v":1,"id":1,"cmd":"speak","args":{"text":"' + b"x" * MSG_MAX + b'"}}\n'
        self.assertFalse(call(audio, big)["ok"])

    def test_state_changes_and_event_log(self):
        self.assertTrue(cmd(self.dir / "audio.sock", "set_volume", {"volume": 55})["ok"])
        self.assertEqual(cmd(self.dir / "audio.sock", "status")["data"]["volume"], 55)
        self.assertTrue(cmd(self.dir / "led.sock", "set_colour", {"r": 1, "g": 2, "b": 3})["ok"])
        self.assertEqual(cmd(self.dir / "led.sock", "status")["data"]["current"]["b"], 3)
        self.assertTrue(cmd(self.dir / "audio.sock", "speak", {"text": "hello"})["ok"])
        log = [json.loads(l) for l in (self.dir / "events.jsonl").read_text().splitlines()]
        self.assertIn({"adapter": "audio", "cmd": "speak", "args": {"text": "hello"}, "ok": True},
                      [{k: e[k] for k in ("adapter", "cmd", "args", "ok")} for e in log])

    def test_scenario_sets_state_and_checks_expectations(self):
        scenario = {"steps": [{"at": 0, "set": {"network": {"signal": 9}}}, {"at": 0.2, "act": "speak"}],
                    "expect": [{"adapter": "audio", "cmd": "speak", "args": {"text": "hi"}, "within_s": 2}]}
        failures = run_scenario(self.sim, scenario, act=lambda _: cmd(self.dir / "audio.sock", "speak", {"text": "hi"}))
        self.assertEqual(failures, [])
        self.assertEqual(cmd(self.dir / "network.sock", "status")["data"]["signal"], 9)
        missing = run_scenario(self.sim, {"expect": [{"adapter": "led", "cmd": "pattern", "within_s": 0.3}]})
        self.assertEqual(len(missing), 1)

    def test_cli_runs_a_shipped_scenario(self):
        result = subprocess.run([sys.executable, str(HERE / "le-hwsim"), "run", str(HERE / "scenarios/network-drop.json"),
                                 "--run-dir", str(self.dir / "cli"), "--timeout", "20"],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scenario passed", result.stdout)


WEB = os.environ.get("LE_HWSIM_WEB_BIN", "")
UI_ROOT = Path(WEB).resolve().parents[1] if WEB else None


@unittest.skipUnless(WEB and os.access(WEB, os.X_OK) and shutil.which("bwrap"), "set LE_HWSIM_WEB_BIN to a host libreecho-web build")
class GoldenWebTests(unittest.TestCase):
    """The real web daemon's linux backend, unmodified, against the simulator."""

    def test_real_web_reads_and_drives_simulated_hardware(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run = tmp / "run"
            run.mkdir()
            port = 18000 + os.getpid() % 1000
            driver = tmp / "driver.sh"
            driver.write_text(f"""
set -eu
python3 {HERE / 'le-hwsim'} serve --run-dir /run/libreecho --log {tmp / 'events.jsonl'} --timeout 60 > {tmp / 'sim.log'} 2>&1 &
sim=$!
i=0; while [ ! -S /run/libreecho/bluetooth.sock ]; do i=$((i+1)); [ $i -lt 100 ] || exit 3; sleep 0.05; done
{WEB} --backend linux --config {tmp / 'config.json'} --web-root {UI_ROOT / 'web'} --listen 127.0.0.1:{port} --dev-controls > {tmp / 'web.log'} 2>&1 &
web=$!
i=0; while ! curl -fsS http://127.0.0.1:{port}/api/v1/config >/dev/null 2>&1; do i=$((i+1)); [ $i -lt 100 ] || exit 4; sleep 0.1; done
CSRF="X-LibreEcho-CSRF: $(curl -fsS http://127.0.0.1:{port}/api/v1/config | sed -n 's/.*"csrf_token":"\\([0-9a-f]*\\)".*/\\1/p')"
for p in audio led network bluetooth; do curl -sS http://127.0.0.1:{port}/api/v1/$p > {tmp}/get-$p.json; done
curl -sS -X PUT -H "$CSRF" -H 'Content-Type: application/json' http://127.0.0.1:{port}/api/v1/audio --data '{{"volume":55}}' > {tmp}/put-audio.json
curl -sS http://127.0.0.1:{port}/api/v1/audio > {tmp}/get-audio-2.json
kill $web $sim; wait $web $sim 2>/dev/null || true
""")
            result = subprocess.run(["bwrap", "--dev-bind", "/", "/", "--tmpfs", "/run", "--bind", str(run), "/run/libreecho",
                                     "--die-with-parent", "sh", str(driver)], capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr + (tmp / "web.log").read_text()[-2000:] if (tmp / "web.log").exists() else result.stderr)
            audio = json.loads((tmp / "get-audio.json").read_text())
            self.assertTrue(audio["ok"], audio)
            self.assertEqual((audio["data"]["volume"], audio["data"]["microphone_gain"], audio["data"]["output_available"]), (40, 65, True))
            led = json.loads((tmp / "get-led.json").read_text())
            self.assertTrue(led["ok"], led)
            self.assertEqual(led["data"]["idle_mode"], "indicator")
            network = json.loads((tmp / "get-network.json").read_text())
            self.assertTrue(network["ok"], network)
            self.assertEqual((network["data"]["ssid"], network["data"]["connectivity"]), ("SimNet", "healthy"))
            bluetooth = json.loads((tmp / "get-bluetooth.json").read_text())
            self.assertTrue(bluetooth["ok"], bluetooth)
            self.assertTrue(json.loads((tmp / "put-audio.json").read_text())["ok"])
            self.assertEqual(json.loads((tmp / "get-audio-2.json").read_text())["data"]["volume"], 55)
            log = [json.loads(l) for l in (tmp / "events.jsonl").read_text().splitlines()]
            self.assertTrue(any(e["adapter"] == "audio" and e["cmd"] == "set_volume" and e["args"] == {"volume": 55} for e in log),
                            "the web daemon's write reached the simulated audio adapter")


if __name__ == "__main__":
    unittest.main()

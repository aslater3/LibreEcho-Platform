#!/usr/bin/env python3
"""The AirPlay bridge never drops audio on a full engine FIFO (issue #356).

A fake slow/blocked engine (this process, reading airplay-media.pcm when it
chooses) sits behind the compiled bridge.  Shairport is a thread writing a
numbered frame stream into airplay.pcm.  Proves:

* a stalled engine loses no bytes and the bridge never closes either FIFO end
  (a closed airplay.pcm is what made Shairport's writes fail with EPIPE);
* the remainder stays a whole number of 4-byte frames;
* hooks are not starved while the bridge waits for FIFO space;
* held audio is discarded only when the session marker is gone or replaced;
* a vanished engine reader is waited out, not turned into loss;
* event lines are rate limited and coalesced, and the status file is a fixed
  size file overwritten in place;
* a flush request from Shairport (skip/seek/pause) drops the unsent remainder
  and the airplay.pcm tail, is answered through the bridge page, leaves the
  session marker and the volume state untouched and does not drop the audio of
  the new track; a request Shairport has already given up on is answered
  without discarding anything;
* Shairport's resync / late-drop / sync-correction counters are copied into the
  status file (still fixed size, overwritten in place) and reported once per
  rate-limit window;
* nothing is written under /data.
"""
import errno
import fcntl
import os
import re
import struct
import subprocess
import tempfile
import termios
import threading
import time
from pathlib import Path

import test_airplay_generation_fence as fence
from test_airplay_session_dsp import HERE

A = "a" * 32
B = "b" * 32
FRAME = 4
STATUS_SIZE = 1024
MAGIC_COUNTERS = 0x3143454C


def frames(start, count, base=0):
    return struct.pack(f"<{count}I", *[(base + start + i) & 0xFFFFFFFF for i in range(count)])


def fionread(fd):
    return struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]


def wait_for(predicate, timeout=5.0, message="timed out"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(message)


def set_marker(root, token, replace=False):
    marker = root / "airplay.active"
    if replace:
        temporary = root / "airplay.active.new"
        temporary.write_text(token + "\n")
        os.rename(temporary, marker)
    else:
        marker.write_text(token + "\n")


class Rig:
    """Bridge + fake engine reader + numbered-frame Shairport writer."""

    def __init__(self, directory, name, cflags=""):
        self.root = directory / name
        self.root.mkdir()
        if cflags:
            os.environ["LE_TEST_BRIDGE_CFLAGS"] = cflags
        try:
            fence.compile_fixture(self.root)
        finally:
            os.environ.pop("LE_TEST_BRIDGE_CFLAGS", None)
        os.mkfifo(self.root / "input.pcm")
        os.mkfifo(self.root / "airplay-media.pcm")
        self.reader = os.open(self.root / "airplay-media.pcm", os.O_RDONLY | os.O_NONBLOCK)
        self.log_path = self.root / "bridge.log"
        self.bridge = subprocess.Popen([str(self.root / "bridge")],
                                       stderr=open(self.log_path, "w"))
        wait_for(lambda: self.fds().get("input"), 5, "bridge never opened airplay.pcm")
        self.writer = os.open(self.root / "input.pcm", os.O_WRONLY)
        self.received = bytearray()
        self.writer_error = None
        self.sent_frames = 0

    def fds(self):
        """The bridge's open FIFO descriptors by name, straight from /proc."""
        found = {}
        for entry in os.listdir(f"/proc/{self.bridge.pid}/fd"):
            try:
                target = os.readlink(f"/proc/{self.bridge.pid}/fd/{entry}")
            except OSError:
                continue
            if target.endswith("/input.pcm"):
                found["input"] = entry
            elif target.endswith("/airplay-media.pcm"):
                found["output"] = entry
        return found

    def log(self):
        return self.log_path.read_text()

    def feed(self, count, base=0, chunk=352):
        """Shairport: blocking writes of numbered frames, in a thread."""
        start = self.sent_frames
        self.sent_frames += count

        def run():
            try:
                done = 0
                while done < count:
                    step = min(chunk, count - done)
                    os.write(self.writer, frames(start + done, step, base))
                    done += step
            except OSError as error:  # EPIPE here means the bridge closed airplay.pcm
                self.writer_error = error
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def drain(self):
        while True:
            try:
                data = os.read(self.reader, 65536)
            except BlockingIOError:
                return
            if not data:
                return
            self.received += data
            assert len(data) % FRAME == 0 or fionread(self.reader) % FRAME == 0

    def close(self):
        for fd in (self.writer, self.reader):
            try:
                os.close(fd)
            except OSError:
                pass
        self.bridge.terminate()
        try:
            self.bridge.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.bridge.kill()


def check_sequence(data, base=0, start=0):
    assert len(data) % FRAME == 0, len(data)
    count = len(data) // FRAME
    values = struct.unpack(f"<{count}I", data)
    for index, value in enumerate(values):
        expected = (base + start + index) & 0xFFFFFFFF
        assert value == expected, f"gap or reorder at frame {index}: got {value}, want {expected}"
    return count


def scenario_stall_loses_nothing(directory):
    rig = Rig(directory, "stall")
    try:
        set_marker(rig.root, A)
        wait_for(lambda: "session-start" in rig.log(), 5, "bridge never saw the session")
        before = rig.fds()
        assert set(before) == {"input", "output"}, before
        total = 60000  # 240 KB: far more than the pipes and FIFO can buffer
        thread = rig.feed(total, chunk=352)
        # The engine is blocked: it reads nothing for 1.5 s (the old bridge gave
        # up and closed airplay.pcm after ~100 ms).
        time.sleep(1.5)
        assert rig.fds() == before, "bridge closed or reopened a FIFO during a stall"
        assert rig.writer_error is None, rig.writer_error
        assert thread.is_alive(), "writer finished: the FIFOs cannot have held 240 KB"
        # The remainder is whole frames, and the engine FIFO only ever holds whole frames.
        assert fionread(rig.reader) % FRAME == 0
        # Hooks are not starved while the bridge waits for space.
        started = time.monotonic()
        assert subprocess.run([str(rig.root / "bridge"), "--set-volume", A, "-12"],
                              timeout=5).returncode == 0
        assert time.monotonic() - started < 0.5, "hook starved behind a blocked engine"
        # The engine wakes up and consumes everything; nothing is missing.
        wait_for(lambda: (rig.drain(), len(rig.received) >= total * FRAME)[1], 20,
                 "stream did not complete")
        thread.join(timeout=5)
        assert not thread.is_alive() and rig.writer_error is None
        assert check_sequence(bytes(rig.received)) == total
        text = rig.log()
        assert "discard" not in text and "reopen" not in text, text
    finally:
        rig.close()


def scenario_partial_frames_are_carried(directory):
    rig = Rig(directory, "carry")
    try:
        set_marker(rig.root, A)
        wait_for(lambda: "session-start" in rig.log())
        stream = frames(0, 3000)
        # Split mid-frame, as a short pipe read could: 6 bytes, then the rest.
        os.write(rig.writer, stream[:6])
        time.sleep(0.2)
        rig.drain()
        # Only whole frames may have been forwarded: 1 of the 1.5 frames sent.
        assert len(rig.received) == 4, len(rig.received)
        os.write(rig.writer, stream[6:5002])
        time.sleep(0.2)
        rig.drain()
        assert len(rig.received) % FRAME == 0
        os.write(rig.writer, stream[5002:])
        wait_for(lambda: (rig.drain(), len(rig.received) == len(stream))[1], 5)
        assert bytes(rig.received) == stream
    finally:
        rig.close()


def scenario_discard_only_when_session_over(directory):
    rig = Rig(directory, "session", "-DEVENT_INTERVAL_MS=100ULL")
    try:
        set_marker(rig.root, A)
        wait_for(lambda: "session-start" in rig.log())
        rig.feed(60000, base=0)
        time.sleep(1.0)  # engine blocked, bridge holding a remainder
        assert "discard" not in rig.log(), rig.log()
        # Session A ends (marker gone): the held remainder is dropped, once.
        (rig.root / "airplay.active").unlink()
        wait_for(lambda: "discard" in rig.log(), 5, "held audio was not discarded at session end")
        assert "session ended" in rig.log()
        assert "session-end" in rig.log()
        # The tail still in airplay.pcm is a dead session's, and is consumed.
        wait_for(lambda: fionread(rig.writer) == 0, 10, "bridge left the dead session's bytes")
        # Whatever the engine did receive for A is an exact prefix: no gap, no loss.
        wait_for(lambda: (rig.drain(), True)[1], 1)
        check_sequence(bytes(rig.received))
        sent_a = len(rig.received) // FRAME
        # Session B: a replacement marker, new stream; none of A's tail may leak in.
        rig.received.clear()
        rig.sent_frames = 0
        set_marker(rig.root, B)
        wait_for(lambda: log_count(rig.log(), "session-start") >= 2, 5)
        base_b = 0x40000000
        rig.feed(5000, base=base_b)
        wait_for(lambda: (rig.drain(), len(rig.received) >= 5000 * FRAME)[1], 10)
        assert check_sequence(bytes(rig.received), base=base_b) == 5000
        assert sent_a < 60000, "session A should have been cut short by the end of the session"

        # Replacement without a gap (marker renamed over): the held remainder of
        # B is dropped as "session replaced", and C starts clean.
        rig.received.clear()
        rig.sent_frames = 0
        rig.feed(60000, base=base_b)
        time.sleep(1.0)
        set_marker(rig.root, "c" * 32, replace=True)
        wait_for(lambda: "session replaced" in rig.log(), 5, "replacement did not drop B's tail")
        wait_for(lambda: log_count(rig.log(), "session-start") >= 3, 5)
        time.sleep(0.3)
        rig.drain()
        # Everything delivered for B is again an exact prefix of B.
        check_sequence(bytes(rig.received), base=base_b)
    finally:
        rig.close()


def log_count(text, word):
    return len(re.findall(word, text))


def scenario_vanished_reader_is_waited_out(directory):
    rig = Rig(directory, "reopen")
    try:
        set_marker(rig.root, A)
        wait_for(lambda: "session-start" in rig.log())
        rig.feed(500)
        wait_for(lambda: (rig.drain(), len(rig.received) == 500 * FRAME)[1], 5)
        before = rig.fds()
        input_fd = before["input"]
        # The engine crashes: the FIFO has no reader at all for a while.
        os.close(rig.reader)
        rig.reader = None
        rig.feed(300)  # frames 500..799 arrive while nobody is listening
        time.sleep(0.6)
        assert rig.fds().get("input") == input_fd, "airplay.pcm must stay open"
        assert rig.writer_error is None
        # The engine returns; the held frames are delivered, none lost.
        rig.reader = os.open(rig.root / "airplay-media.pcm", os.O_RDONLY | os.O_NONBLOCK)
        wait_for(lambda: (rig.drain(), len(rig.received) == 800 * FRAME)[1], 5,
                 "retained frames never arrived after the engine came back")
        assert check_sequence(bytes(rig.received)) == 800
        text = rig.log()
        assert "reopen" in text and "retained" in text, text
        assert "discard" not in text
        assert rig.fds().get("input") == input_fd
    finally:
        if rig.reader is None:
            rig.reader = os.open("/dev/null", os.O_RDONLY)
        rig.close()


def scenario_events_are_rate_limited(directory):
    # Default 10 s window: a burst of stray writes produces one line.
    rig = Rig(directory, "ratelimit")
    try:
        for _ in range(25):  # no marker: each write is a discard
            os.write(rig.writer, frames(0, 100))
            time.sleep(0.03)
        time.sleep(0.3)
        lines = [line for line in rig.log().splitlines() if "discard" in line]
        assert len(lines) == 1, lines
    finally:
        rig.close()
    # A short window shows the coalescing: the rest is reported as one counted line.
    rig = Rig(directory, "coalesce", "-DEVENT_INTERVAL_MS=1000ULL")
    try:
        for _ in range(25):
            os.write(rig.writer, frames(0, 100))
            time.sleep(0.03)
        wait_for(lambda: log_count(rig.log(), "discard x") >= 2, 4, "coalesced line missing")
        counts = [int(m) for m in re.findall(r"discard x(\d+)", rig.log())]
        assert counts[0] == 1 and sum(counts) <= 25 and counts[1] >= 10, counts
        assert len(counts) <= 3, counts  # at most one line per second for 25 events in ~1 s
    finally:
        rig.close()


def read_status(path):
    raw = path.read_bytes()
    assert len(raw) == STATUS_SIZE, f"status file must stay {STATUS_SIZE} bytes, got {len(raw)}"
    assert raw.endswith(b"\n")
    fields = dict(line.split("=", 1) for line in raw.decode().splitlines() if "=" in line)
    return raw, fields


def scenario_status_file_is_fixed_size(directory):
    rig = Rig(directory, "status", "-DSTATUS_INTERVAL_MS=200ULL -DEVENT_INTERVAL_MS=200ULL")
    status = rig.root / "airplay.status"
    try:
        set_marker(rig.root, A)
        wait_for(status.exists, 5, "no status file at session start")
        inode = status.stat().st_ino
        rig.feed(40000)
        seen = set()
        end = time.monotonic() + 2.0
        while time.monotonic() < end:
            raw, fields = read_status(status)
            assert status.stat().st_ino == inode, "status must be overwritten in place"
            assert fields["active"] == "1"
            seen.add(fields["mono_ms"])
            for key in ("pipe_fill_frames", "bridge_remainder_frames", "media_fifo_frames",
                        "engine_queue_frames", "alsa_delay_frames", "downstream_frames",
                        "shairport_delay_frames", "delay_fallbacks", "discards", "reopens",
                        "flushes", "flush_discarded_bytes", "flush_expired", "flush_engine_failed",
                        "shairport_flush_calls", "shairport_flush_timeouts", "shairport_resyncs",
                        "shairport_late_drops", "shairport_frames_inserted",
                        "shairport_frames_dropped"):
                assert key in fields, key
            time.sleep(0.05)
        assert len(seen) >= 4, "status was not refreshed every interval"
        raw, fields = read_status(status)
        assert int(fields["media_fifo_frames"]) > 0 and int(fields["session_frames"]) > 0
        (rig.root / "airplay.active").unlink()
        wait_for(lambda: read_status(status)[1]["active"] == "0", 3, "no final inactive status")
        # No further writes once the session is over: it stops being refreshed.
        stamp = read_status(status)[1]["mono_ms"]
        time.sleep(0.6)
        assert read_status(status)[1]["mono_ms"] == stamp
        assert status.stat().st_size == STATUS_SIZE and status.stat().st_ino == inode
    finally:
        rig.close()


class CountersPage:
    """Shairport's <airplay.pcm>.counters page, written the way its backend does."""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        os.ftruncate(self.fd, 64)
        self.seq = 0
        self.values = dict(mono_ns=0, flush_req=0, flush_calls=0, flush_timeouts=0, resyncs=0,
                           late_drops=0, frames_inserted=0, frames_dropped=0)

    def publish(self, **changes):
        self.values.update(changes)
        v = self.values
        body = struct.pack("<Q8I", v["mono_ns"], v["flush_req"], v["flush_calls"],
                           v["flush_timeouts"], v["resyncs"], v["late_drops"],
                           v["frames_inserted"], v["frames_dropped"], 0)
        self.seq += 2
        os.pwrite(self.fd, struct.pack("<I", self.seq - 1), 4)
        os.pwrite(self.fd, body, 8)
        os.pwrite(self.fd, struct.pack("<I", MAGIC_COUNTERS), 0)
        os.pwrite(self.fd, struct.pack("<I", self.seq), 4)

    def request_flush(self, age_ns=0):
        self.publish(flush_req=self.values["flush_req"] + 1,
                     flush_calls=self.values["flush_calls"] + 1,
                     mono_ns=time.monotonic_ns() - age_ns)
        return self.values["flush_req"]

    def close(self):
        os.close(self.fd)


def bridge_page_fields(root):
    """flush_done, flush_expired, flush_failed from the bridge's delay page."""
    path = root / "input.pcm.delay"
    for _ in range(100):
        raw = path.read_bytes()
        if len(raw) == 64 and struct.unpack("<I", raw[4:8])[0] % 2 == 0:
            return struct.unpack("<III", raw[48:60])
        time.sleep(0.002)
    raise AssertionError("bridge page unreadable")


def file_identity(path):
    st = path.stat()
    return (st.st_dev, st.st_ino, st.st_ctime_ns, st.st_mtime_ns, path.read_bytes())


def scenario_flush_discards_tail_keeps_session(directory):
    rig = Rig(directory, "flush", "-DEVENT_INTERVAL_MS=100ULL")
    shairport = CountersPage(rig.root / "input.pcm.counters")
    try:
        set_marker(rig.root, A)
        (rig.root / "airplay.volume").write_text("-12.000000\n")
        wait_for(lambda: "session-start" in rig.log())
        marker = file_identity(rig.root / "airplay.active")
        volume = file_identity(rig.root / "airplay.volume")
        # The engine is blocked: the stream backs up into the media FIFO, the
        # bridge's remainder and airplay.pcm.
        total = 20000  # 80 KB: fits the FIFO, remainder and airplay.pcm (~45 KB each)
        thread = rig.feed(total, chunk=352)
        thread.join(timeout=10)
        assert not thread.is_alive(), "the pipes should hold 80 KB"
        wait_for(lambda: fionread(rig.reader) >= 40000, 5)
        tail = fionread(rig.writer)
        assert tail > 8192, f"airplay.pcm should hold a backlog, has {tail}"  # stale tail to discard
        # Shairport flushes: the bridge discards remainder + pipe tail and answers.
        request = shairport.request_flush()
        wait_for(lambda: bridge_page_fields(rig.root)[0] == request, 5, "flush was never answered")
        assert fionread(rig.writer) == 0, "airplay.pcm still holds stale audio after the flush"
        assert bridge_page_fields(rig.root)[1:] == (0, 0)
        text = rig.log()
        wait_for(lambda: "flush x1" in rig.log(), 3)
        assert re.search(r"flush x1 \(\d+ bytes dropped \(\d+ bridge, \d+ pipe\)", rig.log()), rig.log()
        assert "discard x" not in text, "a flush is not a session discard"
        # Session state is exactly as before: same marker, same volume file.
        assert file_identity(rig.root / "airplay.active") == marker
        assert file_identity(rig.root / "airplay.volume") == volume
        assert "session-end" not in rig.log() and log_count(rig.log(), "session-start") == 1
        # The new track: its frames follow whatever the media FIFO already held,
        # in order, with none of the discarded tail in between.
        rig.drain()
        old = bytes(rig.received)
        assert len(old) < total * FRAME, "the discarded tail must not reach the engine"
        check_sequence(old)
        rig.received.clear()
        base_b = 0x50000000
        rig.sent_frames = 0
        rig.feed(5000, base=base_b).join(timeout=5)
        wait_for(lambda: (rig.drain(), len(rig.received) >= 5000 * FRAME)[1], 10)
        assert check_sequence(bytes(rig.received), base=base_b) == 5000
        # Flushes and the discarded bytes are in the status file.
        wait_for(lambda: int(read_status(rig.root / "airplay.status")[1].get("flushes", "0")) == 1, 12,
                 "flush missing from airplay.status")
    finally:
        shairport.close()
        rig.close()


def scenario_stale_flush_request_discards_nothing(directory):
    rig = Rig(directory, "flush-late", "-DEVENT_INTERVAL_MS=100ULL")
    shairport = CountersPage(rig.root / "input.pcm.counters")
    try:
        set_marker(rig.root, A)
        wait_for(lambda: "session-start" in rig.log())
        rig.feed(20000, chunk=352).join(timeout=10)
        # Shairport gave up on this request 1 s ago and has been writing since.
        request = shairport.request_flush(age_ns=1_000_000_000)
        wait_for(lambda: bridge_page_fields(rig.root)[0] == request, 5)
        assert bridge_page_fields(rig.root)[1] == 1, "expired request not counted"
        wait_for(lambda: (rig.drain(), len(rig.received) >= 20000 * FRAME)[1], 10)
        assert check_sequence(bytes(rig.received)) == 20000, "an expired flush must not drop audio"
        assert "too late" in rig.log() and "discard x" not in rig.log()
    finally:
        shairport.close()
        rig.close()


def scenario_sync_counters_reach_status(directory):
    rig = Rig(directory, "counters", "-DSTATUS_INTERVAL_MS=200ULL -DEVENT_INTERVAL_MS=1000ULL")
    status = rig.root / "airplay.status"
    shairport = CountersPage(rig.root / "input.pcm.counters")
    try:
        set_marker(rig.root, A)
        wait_for(status.exists, 5)
        inode = status.stat().st_ino
        raw, fields = read_status(status)
        assert fields["shairport_resyncs"] == "0" and fields["flushes"] == "0"
        # Counts that predate nothing: Shairport reports a resync and late drops.
        shairport.publish(resyncs=1, late_drops=4, frames_inserted=30, frames_dropped=12,
                          flush_calls=2, flush_timeouts=1)
        wait_for(lambda: read_status(status)[1]["shairport_resyncs"] == "1", 5)
        raw, fields = read_status(status)
        assert status.stat().st_ino == inode and len(raw) == STATUS_SIZE
        assert (fields["shairport_late_drops"], fields["shairport_frames_inserted"],
                fields["shairport_frames_dropped"], fields["shairport_flush_calls"],
                fields["shairport_flush_timeouts"]) == ("4", "30", "12", "2", "1")
        wait_for(lambda: "resync x5" in rig.log(), 3)
        assert "+1 resyncs, +4 late packets dropped" in rig.log()
        # A burst inside the window is one coalesced line, not one per event.
        for count in range(2, 12):
            shairport.publish(resyncs=count)
            time.sleep(0.02)
        wait_for(lambda: read_status(status)[1]["shairport_resyncs"] == "11", 5)
        time.sleep(1.2)
        lines = [line for line in rig.log().splitlines() if "resync x" in line]
        assert 2 <= len(lines) <= 3, lines
        assert sum(int(m) for m in re.findall(r"resync x(\d+)", rig.log())) == 15, rig.log()
        # A restarted Shairport counts from zero again: no wrapped-around burst.
        shairport.publish(resyncs=0, late_drops=0, frames_inserted=0, frames_dropped=0)
        wait_for(lambda: read_status(status)[1]["shairport_resyncs"] == "0", 5)
        time.sleep(1.2)
        assert sum(int(m) for m in re.findall(r"resync x(\d+)", rig.log())) == 15, rig.log()
        assert status.stat().st_size == STATUS_SIZE and status.stat().st_ino == inode
    finally:
        shairport.close()
        rig.close()


def scenario_nothing_touches_data():
    for name in ("airplay_audio.c", "airplay_delay.h", "shairport-5.6.2-pipe-delay.patch"):
        source = (HERE / name).read_text()
        assert '"/data' not in source, f"{name} must not open a path under /data (RAM-only diagnostics)"
    source = (HERE / "airplay_audio.c").read_text()
    assert 'DEFAULT_AIRPLAY_STATUS_FILE "/run/libreecho-audio/airplay.status"' in source
    assert "O_APPEND" not in source and "O_TRUNC" not in source.split("static void write_status")[1] \
        .split("static void sync_session")[0]


def run():
    with tempfile.TemporaryDirectory(prefix="le-nodrop-") as temp:
        directory = Path(temp)
        scenario_nothing_touches_data()
        scenario_stall_loses_nothing(directory)
        print("bridge: blocked engine loses no frames, FIFOs stay open, hooks not starved")
        scenario_partial_frames_are_carried(directory)
        print("bridge: remainder and carry stay frame aligned")
        scenario_discard_only_when_session_over(directory)
        print("bridge: discard only at session end/replacement")
        scenario_vanished_reader_is_waited_out(directory)
        print("bridge: vanished engine reader waited out without loss")
        scenario_events_are_rate_limited(directory)
        print("bridge: event lines rate limited and coalesced")
        scenario_status_file_is_fixed_size(directory)
        print("bridge: status file fixed size, overwritten in place")
        scenario_flush_discards_tail_keeps_session(directory)
        print("bridge: flush drops the stale tail, keeps marker and volume, new track intact")
        scenario_stale_flush_request_discards_nothing(directory)
        print("bridge: a flush request Shairport gave up on discards nothing")
        scenario_sync_counters_reach_status(directory)
        print("bridge: Shairport sync counters in the fixed-size status, events rate limited")
    print("AirPlay bridge never drops audio on a full pipe: PASS")


if __name__ == "__main__":
    run()

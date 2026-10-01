#!/usr/bin/env python3
"""Compiled LE_AUDIO_SINK/1 framed sink ABI, bounded queue and socket tests.

The test builds three C fixtures against the frozen header/API and exercises
the real implementation:

  * test_audio_sink_unit   - direct API behaviour: bounded per-source queue,
    epoch/generation fencing, fresh OS-random epochs across instances,
    non-consuming render + explicit commit, exact-tail completion driven only
    by an externally supplied physical playhead, negative FINISH totals,
    CANCEL preservation of submitted/played, late-playhead clamping,
    counter-overflow fail-closed arithmetic and the peer credential predicate.
  * test_audio_sink_server - a real SOCK_SEQPACKET server driven here as a
    protocol client for wire round-trips and rejection behaviour.
  * test_audio_sink_epoch_fail - the same source compiled with the epoch
    entropy source forced to fail, proving le_audio_sink_create() fails closed
    instead of falling back to a PID/clock epoch.

Fixtures and sockets live only under the scratch root; no production path such
as /run is touched and no real device is opened.
"""

import os
import select
import socket
import stat
import struct
import subprocess
import tempfile
import time
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent
FIXTURE_ROOT = Path(
    os.environ.get(
        "LE_AUDIO_SINK_FIXTURE_ROOT",
        str(Path(tempfile.gettempdir()) / "sendspin-implementation-sink"),
    )
)

# --- ABI mirrors, kept independent of the C implementation ---------------
MAGIC = 0x314B4E53  # "SNK1" little-endian
VERSION = 1
HEADER_BYTES = 16
MAX_DATAGRAM = 16384
MAX_PAYLOAD = MAX_DATAGRAM - HEADER_BYTES

TYPE_OPEN = 0x01
TYPE_DATA = 0x02
TYPE_PROGRESS_REQ = 0x03
TYPE_FINISH = 0x04
TYPE_CANCEL = 0x05
TYPE_RESET = 0x06
TYPE_OPEN_ACK = 0x81
TYPE_CREDIT = 0x82
TYPE_PROGRESS = 0x83
TYPE_FINISH_ACK = 0x84
TYPE_RESET_ACK = 0x85
TYPE_ERROR = 0x86

OK = 0
ERR_MAGIC = 1
ERR_VERSION = 2
ERR_LENGTH = 3
ERR_RESERVED = 4
ERR_TYPE = 5
ERR_SOURCE = 6
ERR_GEOMETRY = 7
ERR_NO_SESSION = 8
ERR_ALREADY_OPEN = 9
ERR_STALE_EPOCH = 10
ERR_STALE_GENERATION = 11
ERR_SEQUENCE = 12
ERR_FRAME_CURSOR = 13
ERR_FRAME_COUNT = 14
ERR_ALIGNMENT = 15
ERR_CAPACITY = 16
ERR_COUNTER_OVERFLOW = 17
ERR_FINISHED = 18
ERR_PEER = 19

SOURCE_SENDPIN = 1
RATE = 48000
CHANNELS = 2
FMT_S16_LE = 1
BYTES_PER_FRAME = 4
PERIOD_FRAMES = 2048
MAX_CAPACITY = 4096

FLAG_TIMING_VALID = 0x1
FLAG_TIMING_ERROR = 0x2
FLAG_TIMING_INVALID = 0x4
OPEN_READY = 0x1
RESET_HORIZON_VALID = 0x1


def header(type_, length, flags=0, reserved=0, magic=MAGIC, version=VERSION):
    return struct.pack("<IBBHII", magic, version, type_, flags, length, reserved)


def open_payload(generation, source=SOURCE_SENDPIN, rate=RATE,
                 channels=CHANNELS, fmt=FMT_S16_LE, reserved=0):
    return struct.pack("<IIIHHI", source, generation, rate, channels, fmt, reserved)


def data_payload(epoch, generation, sequence, first_frame, frame_count,
                 reserved=0, reserved2=0):
    return struct.pack("<IIIIQII", epoch, generation, sequence, reserved,
                       first_frame, frame_count, reserved2)


def finish_payload(epoch, generation, total, sequence, reserved=0):
    return struct.pack("<IIQII", epoch, generation, total, sequence, reserved)


def cancel_payload(epoch, generation, reason=0, reserved=0):
    return struct.pack("<IIII", epoch, generation, reason, reserved)


def progress_req_payload(epoch, generation):
    return struct.pack("<II", epoch, generation)


def decode(data) -> dict:
    if len(data) < HEADER_BYTES:
        return {"error": "short datagram"}
    magic, version, type_, flags, length, reserved = struct.unpack_from(
        "<IBBHII", data, 0)
    out: dict = {"magic": magic, "version": version, "type": type_, "flags": flags,
           "length": length, "reserved": reserved, "payload": data[HEADER_BYTES:]}
    payload = out["payload"]
    if type_ == TYPE_OPEN_ACK:
        (out["status"], out["epoch"], out["generation"], out["rate"],
         out["channels"], out["format"], out["period_frames"],
         out["capacity_frames"], out["readiness"], out["reserved2"]) = \
            struct.unpack_from("<IIIIHHIIII", payload, 0)
    elif type_ == TYPE_CREDIT:
        (out["status"], out["epoch"], out["generation"], out["sequence"],
         out["accepted_frames"], out["capacity_remaining"], out["reserved2"]) = \
            struct.unpack_from("<IIIIQII", payload, 0)
    elif type_ == TYPE_PROGRESS:
        (out["epoch"], out["generation"], out["sequence"], out["pflags"],
         out["submitted_frames"], out["finish_us"], out["played_frames"],
         out["queued_frames"], out["reserved2"]) = \
            struct.unpack_from("<IIIIQQQII", payload, 0)
    elif type_ == TYPE_FINISH_ACK:
        (out["status"], out["epoch"], out["generation"], out["total_frames"],
         out["completed"], out["reserved2"]) = \
            struct.unpack_from("<IIIQII", payload, 0)
    elif type_ == TYPE_RESET_ACK:
        (out["status"], out["old_epoch"], out["old_generation"],
         out["new_epoch"], out["horizon_frames"], out["rflags"],
         out["reserved2"]) = struct.unpack_from("<IIIIQII", payload, 0)
    elif type_ == TYPE_ERROR:
        (out["status"], out["epoch"], out["generation"], out["detail"],
         out["reserved2"]) = struct.unpack_from("<IIIII", payload, 0)
    return out


class Client:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.sock.settimeout(3.0)
        self.sock.connect(path)

    def send_raw(self, data):
        self.sock.sendall(data)

    def send(self, type_, payload=b""):
        self.send_raw(header(type_, len(payload)) + payload)

    def recv(self):
        return decode(self.sock.recv(65536))

    def request(self, type_, payload=b""):
        self.send(type_, payload)
        return self.recv()

    def close(self):
        self.sock.close()


UNIT_PROGRAM = r'''#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/types.h>

#include "audio_sink.h"
#include "audio_sink_protocol.h"

static int failures;

static void check(int condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "unit FAIL: %s\n", message);
        failures += 1;
    }
}

int main(int argc, char **argv)
{
    struct le_audio_sink_config config;
    struct le_audio_sink *sink;
    struct le_audio_sink_progress progress;
    int16_t frames[LE_AUDIO_SINK_PERIOD_FRAMES * LE_AUDIO_SINK_OUTPUT_CHANNELS];
    int16_t over[(LE_AUDIO_SINK_PERIOD_FRAMES + 1) * LE_AUDIO_SINK_OUTPUT_CHANNELS];
    int16_t out[LE_AUDIO_SINK_PERIOD_FRAMES * LE_AUDIO_SINK_OUTPUT_CHANNELS];
    uint64_t cursor = 0;
    uint64_t first = 12345;
    uint32_t epoch;
    size_t i;

    if (argc < 2)
        return 2;
    for (i = 0; i < sizeof(frames) / sizeof(frames[0]); ++i)
        frames[i] = (int16_t)(i & 0x7fff);

    /* Cumulative frame arithmetic must fail closed on wrap. */
    check(le_audio_sink_cursor_add(UINT64_MAX, 1, &cursor) ==
          LE_AUDIO_SINK_ERR_COUNTER_OVERFLOW, "cursor overflow accepted");
    check(le_audio_sink_cursor_add(UINT64_MAX - 5, 5, &cursor) ==
          LE_AUDIO_SINK_OK && cursor == UINT64_MAX, "cursor exact-max wrong");
    check(le_audio_sink_cursor_add(UINT64_MAX - 5, 6, &cursor) ==
          LE_AUDIO_SINK_ERR_COUNTER_OVERFLOW, "cursor off-by-one overflow");

    /* Peer credentials are an allow-list, not a mere socket-mode check. */
    check(le_audio_sink_peer_allowed(1000, 1000, 0) == 1, "same uid rejected");
    check(le_audio_sink_peer_allowed(1001, 1000, 0) == 0, "foreign uid accepted");
    check(le_audio_sink_peer_allowed(0, 1000, 0) == 0, "root accepted by default");
    check(le_audio_sink_peer_allowed(0, 1000, 1) == 1, "root rejected with opt-in");

    memset(&config, 0, sizeof(config));
    config.socket_path = argv[1];
    config.capacity_frames = LE_AUDIO_SINK_MAX_CAPACITY_FRAMES;
    config.allowed_uid = 1000;
    config.allow_root = 0;
    sink = le_audio_sink_create(&config);
    check(sink != NULL, "create returned NULL");
    if (!sink)
        return 1;
    epoch = le_audio_sink_epoch(sink);
    check(epoch != 0, "epoch is zero");

    /* The epoch must be a fresh OS-random value per instance, never a
     * PID/clock derivation that a restarted engine could repeat. */
    {
        struct le_audio_sink_config config2 = config;
        struct le_audio_sink *sink2;
        char path2[256];
        snprintf(path2, sizeof(path2), "%s.2", argv[1]);
        config2.socket_path = path2;
        sink2 = le_audio_sink_create(&config2);
        check(sink2 != NULL, "second create returned NULL");
        if (sink2) {
            uint32_t epoch2 = le_audio_sink_epoch(sink2);
            check(epoch2 != 0, "second epoch is zero");
            check(epoch2 != epoch, "two instances shared an epoch");
            le_audio_sink_destroy(sink2);
        }
    }

    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.active == 0 && progress.queued_frames == 0,
          "fresh sink is not idle");

    check(le_audio_sink_session_open(sink, LE_AUDIO_SINK_SOURCE_SENDPIN, 1,
                                     LE_AUDIO_SINK_OUTPUT_RATE,
                                     LE_AUDIO_SINK_OUTPUT_CHANNELS,
                                     LE_AUDIO_SINK_FORMAT_S16_LE) ==
          LE_AUDIO_SINK_OK, "session open rejected");

    /* Timing publication is identity/freshness-checked: a stale epoch or
     * generation cannot mark the live session and a reserved flag set is
     * refused.  A valid observation publishes flags+finish; a non-valid state
     * clears the finish horizon. */
    check(le_audio_sink_note_timing(sink, epoch + 1, 1,
                                    LE_AUDIO_SINK_PROGRESS_TIMING_VALID,
                                    999u) == LE_AUDIO_SINK_ERR_STALE_EPOCH,
          "stale-epoch timing accepted");
    check(le_audio_sink_note_timing(sink, epoch, 2,
                                    LE_AUDIO_SINK_PROGRESS_TIMING_VALID,
                                    999u) == LE_AUDIO_SINK_ERR_STALE_GENERATION,
          "stale-generation timing accepted");
    check(le_audio_sink_note_timing(sink, epoch, 1, 0x8u, 999u) ==
          LE_AUDIO_SINK_ERR_RESERVED, "reserved timing flags accepted");
    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          (progress.flags & LE_AUDIO_SINK_PROGRESS_TIMING_INVALID) != 0 &&
          (progress.flags & LE_AUDIO_SINK_PROGRESS_TIMING_VALID) == 0 &&
          progress.finish_us == 0,
          "unpublished timing is not explicitly invalid");
    check(le_audio_sink_note_timing(sink, epoch, 1,
                                    LE_AUDIO_SINK_PROGRESS_TIMING_VALID,
                                    4242u) == LE_AUDIO_SINK_OK,
          "valid timing rejected");
    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          (progress.flags & LE_AUDIO_SINK_PROGRESS_TIMING_VALID) != 0 &&
          (progress.flags & LE_AUDIO_SINK_PROGRESS_TIMING_INVALID) == 0 &&
          progress.finish_us == 4242u,
          "valid timing not published");
    check(le_audio_sink_note_timing(sink, epoch, 1,
                                    LE_AUDIO_SINK_PROGRESS_TIMING_ERROR,
                                    4242u) == LE_AUDIO_SINK_OK,
          "error timing rejected");
    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          (progress.flags & LE_AUDIO_SINK_PROGRESS_TIMING_ERROR) != 0 &&
          progress.finish_us == 0,
          "error timing did not clear the finish horizon");
    check(le_audio_sink_note_timing(sink, epoch, 1,
                                    LE_AUDIO_SINK_PROGRESS_TIMING_INVALID,
                                    0u) == LE_AUDIO_SINK_OK &&
          le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          (progress.flags & LE_AUDIO_SINK_PROGRESS_TIMING_INVALID) != 0 &&
          progress.finish_us == 0,
          "explicit invalid timing not restored");

    /* Stale epoch and generation must fail closed before touching the queue. */
    check(le_audio_sink_submit(sink, (const uint8_t *)frames, 4, 0, epoch + 1, 1, 0) ==
          LE_AUDIO_SINK_ERR_STALE_EPOCH, "stale epoch accepted");
    check(le_audio_sink_submit(sink, (const uint8_t *)frames, 4, 0, epoch, 2, 0) ==
          LE_AUDIO_SINK_ERR_STALE_GENERATION, "stale generation accepted");
    check(le_audio_sink_submit(sink, (const uint8_t *)frames, 4, 0, epoch, 1, 1) ==
          LE_AUDIO_SINK_ERR_SEQUENCE, "out-of-order sequence accepted");
    check(le_audio_sink_submit(sink, (const uint8_t *)frames, 4, 1, epoch, 1, 0) ==
          LE_AUDIO_SINK_ERR_FRAME_CURSOR, "wrong frame cursor accepted");
    check(le_audio_sink_submit(sink, (const uint8_t *)frames, 0, 0, epoch, 1, 0) ==
          LE_AUDIO_SINK_ERR_FRAME_COUNT, "zero frame count accepted");
    check(le_audio_sink_submit(sink, (const uint8_t *)over,
                               LE_AUDIO_SINK_PERIOD_FRAMES + 1, 0, epoch, 1, 0) ==
          LE_AUDIO_SINK_ERR_FRAME_COUNT, "oversize data accepted");
    check(le_audio_sink_submit(sink, NULL, 4, 0, epoch, 1, 0) ==
          LE_AUDIO_SINK_ERR_LENGTH, "null frame pointer accepted");

    /* The queue is bounded; overload is rejected, never grown. */
    check(le_audio_sink_submit(sink, (const uint8_t *)frames,
                               LE_AUDIO_SINK_PERIOD_FRAMES, 0, epoch, 1, 0) ==
          LE_AUDIO_SINK_OK, "first period rejected");
    check(le_audio_sink_submit(sink, (const uint8_t *)frames,
                               LE_AUDIO_SINK_PERIOD_FRAMES,
                               LE_AUDIO_SINK_PERIOD_FRAMES, epoch, 1, 1) ==
          LE_AUDIO_SINK_OK, "second period rejected");
    check(le_audio_sink_submit(sink, (const uint8_t *)frames, 1,
                               2 * LE_AUDIO_SINK_PERIOD_FRAMES, epoch, 1, 2) ==
          LE_AUDIO_SINK_ERR_CAPACITY, "full queue accepted a third period");
    check(le_audio_sink_submit(sink, (const uint8_t *)frames, 1,
                               2 * LE_AUDIO_SINK_PERIOD_FRAMES, epoch, 1, 1) ==
          LE_AUDIO_SINK_ERR_SEQUENCE, "duplicate sequence accepted");

    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.accepted_frames == 2 * LE_AUDIO_SINK_PERIOD_FRAMES &&
          progress.queued_frames == LE_AUDIO_SINK_MAX_CAPACITY_FRAMES &&
          progress.capacity_remaining == 0 &&
          progress.submitted_frames == 0 &&
          (progress.flags & LE_AUDIO_SINK_PROGRESS_TIMING_VALID) == 0 &&
          (progress.flags & LE_AUDIO_SINK_PROGRESS_TIMING_INVALID) != 0 &&
          progress.finish_us == 0,
          "queue/timing accounting wrong");

    /* render() reads without consuming; commit() advances the hardware
     * timeline only after the caller accepted the bytes, and only for the
     * generation whose frames were rendered. */
    memset(out, 0, sizeof(out));
    check(le_audio_sink_render(sink, out, LE_AUDIO_SINK_PERIOD_FRAMES, &first) ==
          LE_AUDIO_SINK_PERIOD_FRAMES && first == 0 &&
          memcmp(out, frames, sizeof(int16_t) * LE_AUDIO_SINK_PERIOD_FRAMES *
                 LE_AUDIO_SINK_OUTPUT_CHANNELS) == 0,
          "render bytes or cursor wrong");
    first = 0;
    check(le_audio_sink_render(sink, out, LE_AUDIO_SINK_PERIOD_FRAMES, &first) ==
          LE_AUDIO_SINK_PERIOD_FRAMES && first == 0,
          "render consumed the queue");

    /* A commit that does not name the live generation is rejected untouched. */
    check(le_audio_sink_commit(sink, epoch, 2, LE_AUDIO_SINK_PERIOD_FRAMES) ==
          LE_AUDIO_SINK_ERR_STALE_GENERATION,
          "commit for a stale generation accepted");
    check(le_audio_sink_commit(sink, epoch + 1, 1, LE_AUDIO_SINK_PERIOD_FRAMES) ==
          LE_AUDIO_SINK_ERR_STALE_EPOCH, "commit with a stale epoch accepted");
    check(le_audio_sink_commit(sink, epoch, 1,
                               2 * LE_AUDIO_SINK_PERIOD_FRAMES + 1) ==
          LE_AUDIO_SINK_ERR_FRAME_COUNT, "over-commit accepted");
    check(le_audio_sink_commit(sink, epoch, 1, LE_AUDIO_SINK_PERIOD_FRAMES) ==
          LE_AUDIO_SINK_OK, "commit rejected");
    check(le_audio_sink_submit(sink, (const uint8_t *)frames, 1,
                               2 * LE_AUDIO_SINK_PERIOD_FRAMES, epoch, 1, 2) ==
          LE_AUDIO_SINK_OK, "capacity not reclaimed after commit");
    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.submitted_frames == LE_AUDIO_SINK_PERIOD_FRAMES &&
          progress.queued_frames == LE_AUDIO_SINK_PERIOD_FRAMES + 1,
          "submitted/queued accounting wrong");

    /* FINISH names the exact accepted total: a high or low total, or a
     * stale epoch/generation, is rejected before any state changes. */
    check(le_audio_sink_finish(sink, epoch, 1,
                               2 * LE_AUDIO_SINK_PERIOD_FRAMES) ==
          LE_AUDIO_SINK_ERR_FRAME_CURSOR, "short FINISH total accepted");
    check(le_audio_sink_finish(sink, epoch, 1,
                               2 * LE_AUDIO_SINK_PERIOD_FRAMES + 2) ==
          LE_AUDIO_SINK_ERR_FRAME_CURSOR, "long FINISH total accepted");
    check(le_audio_sink_finish(sink, epoch + 1, 1,
                               2 * LE_AUDIO_SINK_PERIOD_FRAMES + 1) ==
          LE_AUDIO_SINK_ERR_STALE_EPOCH, "stale-epoch FINISH accepted");
    check(le_audio_sink_finish(sink, epoch, 2,
                               2 * LE_AUDIO_SINK_PERIOD_FRAMES + 1) ==
          LE_AUDIO_SINK_ERR_STALE_GENERATION, "stale-gen FINISH accepted");

    /* FINISH allows an exact short tail and forbids further data. */
    check(le_audio_sink_finish(sink, epoch, 1,
                               2 * LE_AUDIO_SINK_PERIOD_FRAMES + 1) ==
          LE_AUDIO_SINK_OK, "finish rejected");
    check(le_audio_sink_submit(sink, (const uint8_t *)frames, 1,
                               2 * LE_AUDIO_SINK_PERIOD_FRAMES + 1,
                               epoch, 1, 3) == LE_AUDIO_SINK_ERR_FINISHED,
          "data accepted after finish");

    /* Completion is driven only by the generation-tagged physical playhead. */
    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.completed == 0, "completed before any playhead");
    /* An over-submitted value for the live generation is untrustworthy: it is
     * rejected without moving the cursor or fabricating completion. */
    check(le_audio_sink_note_playhead(sink, epoch, 1, UINT64_MAX) ==
          LE_AUDIO_SINK_ERR_FRAME_CURSOR, "over-submitted playhead accepted");
    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.played_frames == 0 && progress.completed == 0,
          "over-submitted playhead fabricated progress");
    check(le_audio_sink_note_playhead(sink, epoch, 1,
                                      LE_AUDIO_SINK_PERIOD_FRAMES) ==
          LE_AUDIO_SINK_OK, "playhead rejected");
    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.played_frames == LE_AUDIO_SINK_PERIOD_FRAMES &&
          progress.completed == 0, "completed without full playhead");
    /* A mismatched epoch/generation is a no-op regardless of the value. */
    check(le_audio_sink_note_playhead(sink, epoch, 2,
                                      LE_AUDIO_SINK_PERIOD_FRAMES + 1) ==
          LE_AUDIO_SINK_ERR_STALE_GENERATION,
          "stale-generation playhead accepted");
    check(le_audio_sink_note_playhead(sink, epoch + 1, 1,
                                      LE_AUDIO_SINK_PERIOD_FRAMES + 1) ==
          LE_AUDIO_SINK_ERR_STALE_EPOCH, "stale-epoch playhead accepted");
    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.played_frames == LE_AUDIO_SINK_PERIOD_FRAMES &&
          progress.completed == 0, "mismatched playhead moved state");
    check(le_audio_sink_commit(sink, epoch, 1, LE_AUDIO_SINK_PERIOD_FRAMES + 1) ==
          LE_AUDIO_SINK_OK, "tail commit rejected");
    check(le_audio_sink_note_playhead(sink, epoch, 1,
                                      2 * LE_AUDIO_SINK_PERIOD_FRAMES + 1) ==
          LE_AUDIO_SINK_OK, "final playhead rejected");
    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.completed == 1 &&
          progress.played_frames == 2 * LE_AUDIO_SINK_PERIOD_FRAMES + 1,
          "completion not driven by the physical playhead");
    check(le_audio_sink_note_playhead(sink, epoch, 1, 5) == LE_AUDIO_SINK_OK &&
          le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.played_frames == 2 * LE_AUDIO_SINK_PERIOD_FRAMES + 1,
          "playhead moved backwards");

    /* CANCEL discards only this generation's unrendered frames and fences it. */
    check(le_audio_sink_cancel(sink, epoch, 1) == LE_AUDIO_SINK_OK,
          "cancel rejected");
    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.active == 0 && progress.finished == 0 &&
          progress.completed == 0 && progress.queued_frames == 0 &&
          progress.submitted_frames == 2 * LE_AUDIO_SINK_PERIOD_FRAMES + 1 &&
          progress.played_frames == 2 * LE_AUDIO_SINK_PERIOD_FRAMES + 1,
          "cancel did not preserve submitted/played");
    /* A late playhead or commit for the fenced generation is rejected. */
    check(le_audio_sink_note_playhead(sink, epoch, 1, UINT64_MAX) ==
          LE_AUDIO_SINK_ERR_STALE_GENERATION,
          "late playhead after cancel accepted");
    check(le_audio_sink_commit(sink, epoch, 1, 0) ==
          LE_AUDIO_SINK_ERR_STALE_GENERATION,
          "late commit after cancel accepted");
    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.played_frames == 2 * LE_AUDIO_SINK_PERIOD_FRAMES + 1 &&
          progress.completed == 0,
          "late playhead after cancel moved state");
    check(le_audio_sink_submit(sink, (const uint8_t *)frames, 1,
                               2 * LE_AUDIO_SINK_PERIOD_FRAMES + 1,
                               epoch, 1, 3) == LE_AUDIO_SINK_ERR_STALE_GENERATION,
          "cancelled generation still admits data");
    check(le_audio_sink_cancel(sink, epoch, 1) ==
          LE_AUDIO_SINK_ERR_STALE_GENERATION,
          "re-cancel of fenced generation accepted");
    check(le_audio_sink_finish(sink, epoch, 1, 0) ==
          LE_AUDIO_SINK_ERR_STALE_GENERATION,
          "FINISH for fenced generation accepted");
    check(le_audio_sink_session_open(sink, LE_AUDIO_SINK_SOURCE_SENDPIN, 2,
                                     LE_AUDIO_SINK_OUTPUT_RATE,
                                     LE_AUDIO_SINK_OUTPUT_CHANNELS,
                                     LE_AUDIO_SINK_FORMAT_S16_LE) ==
          LE_AUDIO_SINK_OK, "successor generation rejected");
    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.active == 1 && progress.submitted_frames == 0 &&
          progress.played_frames == 0 && progress.queued_frames == 0,
          "successor generation did not reset progress");
    /* A published timing observation must not leak across a generation
     * boundary: the successor starts explicitly invalid. */
    check((progress.flags & LE_AUDIO_SINK_PROGRESS_TIMING_INVALID) != 0 &&
          progress.finish_us == 0,
          "timing leaked across the generation boundary");
    check(le_audio_sink_session_open(sink, LE_AUDIO_SINK_SOURCE_SENDPIN, 1,
                                     LE_AUDIO_SINK_OUTPUT_RATE,
                                     LE_AUDIO_SINK_OUTPUT_CHANNELS,
                                     LE_AUDIO_SINK_FORMAT_S16_LE) ==
          LE_AUDIO_SINK_ERR_STALE_GENERATION, "regressed generation accepted");
    check(le_audio_sink_submit(sink, (const uint8_t *)frames, 4, 0, epoch, 2, 0) ==
          LE_AUDIO_SINK_OK, "successor data rejected");
    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.queued_frames == 4 && progress.accepted_frames == 4,
          "successor accounting wrong");

    /* F2 regression: the successor FINISHes and commits 4 frames, then a late
     * playhead from the fenced predecessor (an absolute value above the
     * successor's submitted count) must not move or complete the successor. */
    check(le_audio_sink_finish(sink, epoch, 2, 4) == LE_AUDIO_SINK_OK,
          "successor finish rejected");
    check(le_audio_sink_commit(sink, epoch, 2, 4) == LE_AUDIO_SINK_OK,
          "successor commit rejected");
    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.submitted_frames == 4 && progress.played_frames == 0 &&
          progress.completed == 0, "successor pre-callback state wrong");
    check(le_audio_sink_note_playhead(sink, epoch, 1, 1000000ULL) ==
          LE_AUDIO_SINK_ERR_STALE_GENERATION,
          "stale predecessor playhead accepted");
    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.played_frames == 0 && progress.completed == 0,
          "stale predecessor playhead moved successor");
    check(le_audio_sink_note_playhead(sink, epoch + 1, 2, 4) ==
          LE_AUDIO_SINK_ERR_STALE_EPOCH,
          "stale-epoch successor playhead accepted");
    check(le_audio_sink_note_playhead(sink, epoch, 2, 1000000ULL) ==
          LE_AUDIO_SINK_ERR_FRAME_CURSOR,
          "over-submitted successor playhead accepted");
    check(le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.played_frames == 0 && progress.completed == 0,
          "over-submitted successor playhead fabricated completion");
    check(le_audio_sink_note_playhead(sink, epoch, 2, 2) == LE_AUDIO_SINK_OK &&
          le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.played_frames == 2 && progress.completed == 0,
          "successor partial playhead wrong");
    check(le_audio_sink_note_playhead(sink, epoch, 2, 4) == LE_AUDIO_SINK_OK &&
          le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.played_frames == 4 && progress.completed == 1,
          "successor completion not driven by its own cursor");
    check(le_audio_sink_note_playhead(sink, epoch, 2, 1) == LE_AUDIO_SINK_OK &&
          le_audio_sink_get_progress(sink, &progress) == LE_AUDIO_SINK_OK &&
          progress.played_frames == 4,
          "successor cursor moved backwards");

    /* A byte payload with no alignment guarantee is decoded byte-for-byte:
     * submitting from an odd address must yield the same samples. */
    check(le_audio_sink_session_open(sink, LE_AUDIO_SINK_SOURCE_SENDPIN, 3,
                                     LE_AUDIO_SINK_OUTPUT_RATE,
                                     LE_AUDIO_SINK_OUTPUT_CHANNELS,
                                     LE_AUDIO_SINK_FORMAT_S16_LE) ==
          LE_AUDIO_SINK_OK, "unaligned-successor open rejected");
    {
        uint8_t blob[8 * LE_AUDIO_SINK_BYTES_PER_FRAME + 1];
        uint8_t *odd = blob + 1;   /* deliberately misaligned */
        int16_t expected[8 * LE_AUDIO_SINK_OUTPUT_CHANNELS];
        size_t k;

        for (k = 0; k < sizeof(expected) / sizeof(expected[0]); ++k) {
            int16_t sample = (int16_t)((int)(k * 37) & 0x7fff);
            expected[k] = sample;
            odd[2 * k] = (uint8_t)(sample & 0xff);
            odd[2 * k + 1] = (uint8_t)(((uint16_t)sample >> 8) & 0xff);
        }
        check(le_audio_sink_submit(sink, odd, 8, 0, epoch, 3, 0) ==
              LE_AUDIO_SINK_OK, "unaligned byte submit rejected");
        memset(out, 0, sizeof(out));
        first = 12345;
        check(le_audio_sink_render(sink, out, 8, &first) == 8 && first == 0 &&
              memcmp(out, expected, sizeof(expected)) == 0,
              "unaligned byte decode wrong");
    }

    le_audio_sink_destroy(sink);
    if (failures)
        return 1;
    puts("audio_sink unit: bounded queue, fencing, playhead completion PASS");
    return 0;
}
'''

SERVER_PROGRAM = r'''
#define _GNU_SOURCE
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "audio_sink.h"

static volatile sig_atomic_t stopping;

static void on_signal(int signo)
{
    (void)signo;
    stopping = 1;
}

int main(int argc, char **argv)
{
    struct le_audio_sink_config config;
    struct le_audio_sink *sink;
    uint64_t publish_finish = 0;
    int published = 0;

    if (argc < 3)
        return 2;
    if (argc >= 4)
        publish_finish = (uint64_t)strtoull(argv[3], NULL, 10);
    memset(&config, 0, sizeof(config));
    config.socket_path = argv[1];
    config.capacity_frames = (uint32_t)strtoul(argv[2], NULL, 10);
    config.allowed_uid = (uid_t)-1;   /* server euid */
    config.allow_root = 1;
    sink = le_audio_sink_create(&config);
    if (!sink) {
        fprintf(stderr, "server: create failed\n");
        return 1;
    }
    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);
    puts("READY");
    fflush(stdout);
    while (!stopping) {
        if (le_audio_sink_service(sink) < 0)
            break;
        /* Test-only: when asked, publish one verified timing observation for
         * the first live generation so the wire PROGRESS path can be asserted.
         * This exercises the real engine->sink publication API, not a stub. */
        if (publish_finish != 0 && !published) {
            struct le_audio_sink_progress progress;

            if (le_audio_sink_get_progress(sink, &progress) ==
                    LE_AUDIO_SINK_OK &&
                progress.active &&
                le_audio_sink_note_timing(
                    sink, progress.epoch, progress.generation,
                    LE_AUDIO_SINK_PROGRESS_TIMING_VALID,
                    publish_finish) == LE_AUDIO_SINK_OK)
                published = 1;
        }
        usleep(500);
    }
    le_audio_sink_destroy(sink);
    return 0;
}
'''

# Compiled with -DLE_AUDIO_SINK_TEST_FORCE_EPOCH_ENTROPY_FAILURE so the
# "no reliable epoch source" path is exercised on a real instance.
EPOCH_FAIL_PROGRAM = r'''
#define _GNU_SOURCE
#include <stdio.h>
#include <string.h>
#include <sys/types.h>

#include "audio_sink.h"

int main(int argc, char **argv)
{
    struct le_audio_sink_config config;
    struct le_audio_sink *sink;

    if (argc < 2)
        return 2;
    memset(&config, 0, sizeof(config));
    config.socket_path = argv[1];
    config.capacity_frames = LE_AUDIO_SINK_MAX_CAPACITY_FRAMES;
    config.allowed_uid = 1000;
    sink = le_audio_sink_create(&config);
    if (sink != NULL) {
        fprintf(stderr, "epoch failure: create did not fail closed\n");
        le_audio_sink_destroy(sink);
        return 1;
    }
    puts("audio_sink epoch: missing entropy fails closed PASS");
    return 0;
}
'''

OWNER_PROGRAM = r'''#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

#include "audio_sink.h"

static volatile sig_atomic_t stopping;

static void on_signal(int signo)
{
    (void)signo;
    stopping = 1;
}

static int make_sink(const char *path, struct le_audio_sink **out)
{
    struct le_audio_sink_config config;

    memset(&config, 0, sizeof(config));
    config.socket_path = path;
    config.capacity_frames = 256;
    config.allowed_uid = (uid_t)-1;
    config.allow_root = 1;
    *out = le_audio_sink_create(&config);
    return *out ? 0 : -1;
}

int main(int argc, char **argv)
{
    const char *mode;
    const char *path;

    if (argc < 3)
        return 2;
    mode = argv[1];
    path = argv[2];

    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);

    if (strcmp(mode, "try") == 0) {
        struct le_audio_sink *sink;
        struct stat st;

        if (make_sink(path, &sink) != 0) {
            puts("TRY NULL");
            return 0;
        }
        if (stat(path, &st) == 0)
            printf("TRY NONNULL %llu\n", (unsigned long long)st.st_ino);
        else
            puts("TRY NONNULL none");
        le_audio_sink_destroy(sink);
        return 0;
    }
    if (strcmp(mode, "hold") == 0) {
        struct le_audio_sink *sink;
        struct stat st;

        if (make_sink(path, &sink) != 0) {
            puts("HOLD FAILED");
            return 1;
        }
        if (stat(path, &st) != 0) {
            perror("hold stat");
            le_audio_sink_destroy(sink);
            return 1;
        }
        printf("READY %llu\n", (unsigned long long)st.st_ino);
        fflush(stdout);
        while (!stopping) {
            le_audio_sink_service(sink);
            usleep(1000);
        }
        le_audio_sink_destroy(sink);
        puts("DESTROYED");
        fflush(stdout);
        return 0;
    }
    if (strcmp(mode, "holdraw") == 0) {
        struct sockaddr_un addr;
        struct stat st;
        int fd;

        unlink(path);
        fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
        if (fd < 0) {
            perror("holdraw socket");
            return 1;
        }
        if (strlen(path) >= sizeof(addr.sun_path)) {
            close(fd);
            return 1;
        }
        memset(&addr, 0, sizeof(addr));
        addr.sun_family = AF_UNIX;
        memcpy(addr.sun_path, path, strlen(path) + 1);
        if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0 ||
            chmod(path, 0600) != 0 || listen(fd, 4) != 0) {
            perror("holdraw bind");
            close(fd);
            return 1;
        }
        if (stat(path, &st) != 0) {
            perror("holdraw stat");
            close(fd);
            return 1;
        }
        printf("READY %llu\n", (unsigned long long)st.st_ino);
        fflush(stdout);
        while (!stopping) {
            int peer = accept4(fd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
            if (peer >= 0)
                close(peer);
            usleep(1000);
        }
        close(fd);
        unlink(path);
        puts("DESTROYED");
        fflush(stdout);
        return 0;
    }
    if (strcmp(mode, "mkstale") == 0) {
        struct sockaddr_un addr;
        int fd;

        unlink(path);
        fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
        if (fd < 0)
            return 1;
        if (strlen(path) >= sizeof(addr.sun_path)) {
            close(fd);
            return 1;
        }
        memset(&addr, 0, sizeof(addr));
        addr.sun_family = AF_UNIX;
        memcpy(addr.sun_path, path, strlen(path) + 1);
        if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
            perror("mkstale bind");
            close(fd);
            return 1;
        }
        close(fd);   /* leave the dead node behind: no listener */
        puts("STALE");
        return 0;
    }
    return 2;
}
'''



def compile_fixture(cc, name, program, root, extra_flags=()):
    source = root / (name + ".c")
    binary = root / name
    source.write_text(program, encoding="ascii")
    subprocess.run(
        [cc, "-std=c99", "-Wall", "-Wextra", "-Wpedantic", "-Werror",
         "-I", str(SOURCE_DIR), *extra_flags, str(source),
         str(SOURCE_DIR / "audio_sink.c"), "-o", str(binary)],
        check=True, timeout=120)
    return binary



def wait_ready(process, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        ready, _, _ = select.select([process.stdout], [], [], 0.2)
        if ready:
            line = process.stdout.readline()
            if line.strip() == "READY":
                return
            if line == "":
                break
    raise AssertionError("sink server did not become ready")


def expect(reply, status, label):
    assert reply.get("type") == TYPE_ERROR, \
        f"{label}: expected ERROR, got type {reply.get('type')}"
    assert reply.get("status") == status, \
        f"{label}: expected status {status}, got {reply.get('status')}"


def run_socket_suite(server_path, socket_path):
    assert len(str(socket_path)) < 100, "socket path too long for sun_path"
    stat = os.stat(socket_path)
    assert stat.st_uid == os.geteuid(), "socket not owned by the engine uid"
    assert stat.st_mode & 0o777 == 0o600, \
        f"socket mode {oct(stat.st_mode & 0o777)} is not restrictive"

    client = Client(str(socket_path))
    epoch = None

    # OPEN -> OPEN_ACK with the engine epoch, geometry and readiness.
    reply = client.request(TYPE_OPEN, open_payload(1))
    assert reply["type"] == TYPE_OPEN_ACK and reply["status"] == OK, reply
    epoch = reply["epoch"]
    assert epoch != 0
    assert (reply["rate"], reply["channels"], reply["format"]) == \
        (RATE, CHANNELS, FMT_S16_LE), reply
    assert reply["period_frames"] == PERIOD_FRAMES
    assert reply["capacity_frames"] == MAX_CAPACITY, reply
    assert reply["readiness"] & OPEN_READY

    # DATA -> CREDIT reports cumulative accepted frames and remaining capacity.
    reply = client.request(
        TYPE_DATA, data_payload(epoch, 1, 0, 0, PERIOD_FRAMES) + bytes(PERIOD_FRAMES * BYTES_PER_FRAME))
    assert reply["type"] == TYPE_CREDIT and reply["status"] == OK, reply
    assert reply["accepted_frames"] == PERIOD_FRAMES
    assert reply["capacity_remaining"] == MAX_CAPACITY - PERIOD_FRAMES
    reply = client.request(
        TYPE_DATA, data_payload(epoch, 1, 1, PERIOD_FRAMES, PERIOD_FRAMES) +
        bytes(PERIOD_FRAMES * BYTES_PER_FRAME))
    assert reply["accepted_frames"] == 2 * PERIOD_FRAMES
    assert reply["capacity_remaining"] == 0

    # Full queue: overload is rejected, never grown.
    expect(client.request(
        TYPE_DATA, data_payload(epoch, 1, 2, 2 * PERIOD_FRAMES, PERIOD_FRAMES) +
        bytes(PERIOD_FRAMES * BYTES_PER_FRAME)), ERR_CAPACITY, "queue full")

    # Duplicate and out-of-order sequences fail closed.
    expect(client.request(
        TYPE_DATA, data_payload(epoch, 1, 1, 2 * PERIOD_FRAMES, 1) + bytes(4)),
        ERR_SEQUENCE, "duplicate sequence")
    expect(client.request(
        TYPE_DATA, data_payload(epoch, 1, 5, 2 * PERIOD_FRAMES, 1) + bytes(4)),
        ERR_SEQUENCE, "out-of-order sequence")

    # Stale epoch and stale generation never touch the live session.
    expect(client.request(
        TYPE_DATA, data_payload(epoch + 1, 1, 2, 2 * PERIOD_FRAMES, 1) + bytes(4)),
        ERR_STALE_EPOCH, "stale epoch")
    expect(client.request(
        TYPE_DATA, data_payload(epoch, 2, 2, 2 * PERIOD_FRAMES, 1) + bytes(4)),
        ERR_STALE_GENERATION, "stale generation")

    # PROGRESS exposes explicit invalid timing until the engine supplies a model.
    reply = client.request(TYPE_PROGRESS_REQ, progress_req_payload(epoch, 1))
    assert reply["type"] == TYPE_PROGRESS, reply
    assert reply["queued_frames"] == 2 * PERIOD_FRAMES, reply
    assert reply["pflags"] & FLAG_TIMING_VALID == 0
    assert reply["pflags"] & FLAG_TIMING_INVALID != 0
    assert reply["finish_us"] == 0

    # FINISH must name the exact accepted total: high and low totals, and a
    # sequence that is not the last accepted one, are rejected before any
    # state changes.
    expect(client.request(
        TYPE_FINISH, finish_payload(epoch, 1, 2 * PERIOD_FRAMES - 1, 1)),
        ERR_FRAME_CURSOR, "FINISH total too low")
    expect(client.request(
        TYPE_FINISH, finish_payload(epoch, 1, 2 * PERIOD_FRAMES + 1, 1)),
        ERR_FRAME_CURSOR, "FINISH total too high")
    expect(client.request(
        TYPE_FINISH, finish_payload(epoch, 1, 2 * PERIOD_FRAMES, 0)),
        ERR_SEQUENCE, "FINISH stale sequence")
    # Malformed FINISH framing fails closed.
    client.send_raw(header(TYPE_FINISH, 20) + finish_payload(epoch, 1, 0, 1))
    expect(client.recv(), ERR_LENGTH, "FINISH bad length")
    client.send_raw(header(TYPE_FINISH, 24) +
                    finish_payload(epoch, 1, 0, 1, reserved=1))
    expect(client.recv(), ERR_RESERVED, "FINISH reserved")

    # FINISH accepts the exact total and does not claim physical completion.
    reply = client.request(
        TYPE_FINISH, finish_payload(epoch, 1, 2 * PERIOD_FRAMES, 1))
    assert reply["type"] == TYPE_FINISH_ACK and reply["status"] == OK, reply
    assert reply["total_frames"] == 2 * PERIOD_FRAMES
    assert reply["completed"] == 0
    expect(client.request(
        TYPE_DATA, data_payload(epoch, 1, 2, 2 * PERIOD_FRAMES, 1) + bytes(4)),
        ERR_FINISHED, "data after finish")

    # CANCEL fences the generation and reports an invalid hardware horizon.
    reply = client.request(TYPE_CANCEL, cancel_payload(epoch, 1))
    assert reply["type"] == TYPE_RESET_ACK and reply["status"] == OK, reply
    assert reply["old_generation"] == 1
    assert reply["rflags"] & RESET_HORIZON_VALID == 0
    assert reply["horizon_frames"] == 0
    expect(client.request(
        TYPE_DATA, data_payload(epoch, 1, 2, 2 * PERIOD_FRAMES, 1) + bytes(4)),
        ERR_STALE_GENERATION, "fenced generation")
    # A repeated terminal for the fenced generation stays fenced.
    expect(client.request(TYPE_CANCEL, cancel_payload(epoch, 1)),
           ERR_STALE_GENERATION, "re-cancel of fenced generation")
    expect(client.request(
        TYPE_FINISH, finish_payload(epoch, 1, 2 * PERIOD_FRAMES, 1)),
        ERR_STALE_GENERATION, "FINISH for fenced generation")

    # A successor generation opens normally and starts from frame zero.
    reply = client.request(TYPE_OPEN, open_payload(2))
    assert reply["type"] == TYPE_OPEN_ACK and reply["status"] == OK, reply
    assert reply["epoch"] == epoch
    reply = client.request(
        TYPE_DATA, data_payload(epoch, 2, 0, 0, 1024) + bytes(1024 * BYTES_PER_FRAME))
    assert reply["type"] == TYPE_CREDIT and reply["accepted_frames"] == 1024, reply
    assert reply["capacity_remaining"] == MAX_CAPACITY - 1024
    reply = client.request(TYPE_FINISH, finish_payload(epoch, 2, 1024, 0))
    assert reply["type"] == TYPE_FINISH_ACK and reply["completed"] == 0, reply

    # A regressed generation is refused.
    expect(client.request(TYPE_OPEN, open_payload(1)),
           ERR_STALE_GENERATION, "regressed generation")

    # Malformed datagrams fail closed with no silent acceptance.
    client.send_raw(header(TYPE_OPEN, 20, magic=0) + open_payload(3))
    expect(client.recv(), ERR_MAGIC, "bad magic")
    client.send_raw(header(TYPE_OPEN, 20, version=2) + open_payload(3))
    expect(client.recv(), ERR_VERSION, "bad version")
    client.send_raw(header(TYPE_OPEN, 20, flags=1) + open_payload(3))
    expect(client.recv(), ERR_RESERVED, "header flags")
    client.send_raw(header(TYPE_OPEN, 20, reserved=1) + open_payload(3))
    expect(client.recv(), ERR_RESERVED, "header reserved")
    client.send_raw(header(0x40, 0))
    expect(client.recv(), ERR_TYPE, "unknown type")
    client.send_raw(header(TYPE_OPEN, 20) + open_payload(3, source=99))
    expect(client.recv(), ERR_SOURCE, "bad source")
    client.send_raw(header(TYPE_OPEN, 20) + open_payload(3, rate=44100))
    expect(client.recv(), ERR_GEOMETRY, "bad geometry")
    client.send_raw(header(TYPE_OPEN, 20) + open_payload(3, channels=1))
    expect(client.recv(), ERR_GEOMETRY, "bad channels")
    client.send_raw(header(TYPE_OPEN, 18) + open_payload(3))
    expect(client.recv(), ERR_LENGTH, "bad open length")
    client.send_raw(header(TYPE_OPEN, 20) + open_payload(3, reserved=1))
    expect(client.recv(), ERR_RESERVED, "open reserved")

    # Open a clean session for the DATA framing checks.
    reply = client.request(TYPE_OPEN, open_payload(3))
    assert reply["type"] == TYPE_OPEN_ACK and reply["status"] == OK, reply

    # Malformed CANCEL/RESET framing is rejected before the session is touched.
    client.send_raw(header(TYPE_CANCEL, 12) + cancel_payload(epoch, 3))
    expect(client.recv(), ERR_LENGTH, "CANCEL bad length")
    client.send_raw(header(TYPE_CANCEL, 16) + cancel_payload(epoch, 3, reserved=1))
    expect(client.recv(), ERR_RESERVED, "CANCEL reserved")
    client.send_raw(header(TYPE_RESET, 12) + cancel_payload(epoch, 3))
    expect(client.recv(), ERR_LENGTH, "RESET bad length")
    client.send_raw(header(TYPE_RESET, 16) + cancel_payload(epoch, 3, reserved=1))
    expect(client.recv(), ERR_RESERVED, "RESET reserved")

    # Wrong first-frame cursor.
    expect(client.request(
        TYPE_DATA, data_payload(epoch, 3, 0, 5, 1) + bytes(4)),
        ERR_FRAME_CURSOR, "wrong cursor")
    # Truncated datagram whose header length disagrees with the payload.
    client.send_raw(header(TYPE_DATA, 40) + data_payload(epoch, 3, 0, 0, 1))
    expect(client.recv(), ERR_LENGTH, "truncated data")
    # Frame misalignment: payload is not 32 + 4 * frame_count bytes.
    client.send_raw(header(TYPE_DATA, 34) + data_payload(epoch, 3, 0, 0, 4) + bytes(2))
    expect(client.recv(), ERR_ALIGNMENT, "misaligned payload")
    # Oversize frame count within a self-consistent payload.
    client.send_raw(header(TYPE_DATA, 32 + (PERIOD_FRAMES + 1) * BYTES_PER_FRAME) +
                    data_payload(epoch, 3, 0, 0, PERIOD_FRAMES + 1) +
                    bytes((PERIOD_FRAMES + 1) * BYTES_PER_FRAME))
    expect(client.recv(), ERR_FRAME_COUNT, "oversize frame count")
    # Oversize datagram beyond the maximum the server will accept.
    huge = header(TYPE_DATA, 20000) + bytes(20000)
    client.send_raw(huge)
    expect(client.recv(), ERR_LENGTH, "oversize datagram")
    # Reserved DATA prefix fields must be zero.
    client.send_raw(header(TYPE_DATA, 32) + data_payload(epoch, 3, 0, 0, 0, reserved2=1))
    expect(client.recv(), ERR_RESERVED, "data reserved")

    # One bounded active client: a second connection cannot complete an OPEN.
    intruder = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    intruder.settimeout(2.0)
    try:
        intruder.connect(str(socket_path))
        intruder.sendall(header(TYPE_OPEN, 20) + open_payload(9))
        try:
            data = intruder.recv(65536)
        except (ConnectionResetError, socket.timeout):
            data = b""
        assert data == b"", "second client was admitted while one was active"
    finally:
        intruder.close()

    client.close()

    # Peer disappearance releases ownership; a reconnect gets a fresh session.
    client = Client(str(socket_path))
    reply = client.request(TYPE_OPEN, open_payload(4))
    assert reply["type"] == TYPE_OPEN_ACK and reply["epoch"] == epoch, reply
    reply = client.request(TYPE_PROGRESS_REQ, progress_req_payload(epoch, 4))
    assert reply["type"] == TYPE_PROGRESS and reply["queued_frames"] == 0, reply
    client.close()
    print("audio_sink socket: LE_AUDIO_SINK/1 round-trips and rejections PASS")

def read_owner_line(process, timeout=10.0):
    """Read one stdout line from a long-running owner fixture, bounded."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], 0.1)
        if ready:
            line = process.stdout.readline()
            if line == "":
                raise AssertionError("owner process closed stdout early")
            return line.strip()
    raise AssertionError("owner process produced no output before timeout")


def start_owner(owner, mode, path, timeout=10.0):
    process = subprocess.Popen(
        [str(owner), mode, str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    line = read_owner_line(process, timeout)
    return process, line


def stop_owner(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)
    return process.returncode


def run_owner_try(owner, path):
    return subprocess.run(
        [str(owner), "try", str(path)],
        capture_output=True, text=True, timeout=30)


def run_ownership_suite(owner):
    """Socket-path ownership: fail closed on live/non-socket collisions and
    never unlink a node this instance does not own (F1 regression suite)."""
    with tempfile.TemporaryDirectory(prefix="own", dir=str(FIXTURE_ROOT)) as temp:
        root = Path(temp)

        # A regular file at the socket path must be refused and left intact.
        regular = root / "regular.sock"
        secret = b"SECRET-DATA-NOT-A-SOCKET"
        regular.write_bytes(secret)
        result = run_owner_try(owner, regular)
        assert result.stdout.startswith("TRY NULL"), result.stdout
        assert regular.is_file() and not regular.is_socket(), \
            "regular file was replaced or removed"
        assert regular.read_bytes() == secret, "regular file content destroyed"

        # A symlink at the socket path must be refused and left intact.
        target = root / "target.txt"
        target.write_bytes(b"SYMLINK-TARGET-KEEP")
        link = root / "link.sock"
        link.symlink_to(target)
        result = run_owner_try(owner, link)
        assert result.stdout.startswith("TRY NULL"), result.stdout
        assert link.is_symlink() and os.readlink(link) == str(target), \
            "symlink was replaced"
        assert target.read_bytes() == b"SYMLINK-TARGET-KEEP", "symlink target clobbered"

        # A cooperating live holder owning the path: a second create fails
        # closed and the bound node is untouched.
        live = root / "live.sock"
        holder, line = start_owner(owner, "hold", live)
        assert line.startswith("READY "), line
        live_ino = int(line.split()[1])
        result = run_owner_try(owner, live)
        assert result.stdout.startswith("TRY NULL"), result.stdout
        assert os.stat(live).st_ino == live_ino, "live node was replaced"
        assert holder.poll() is None, "holder died during collision"
        client = Client(str(live))
        reply = client.request(TYPE_OPEN, open_payload(1))
        assert reply["type"] == TYPE_OPEN_ACK and reply["status"] == OK, reply
        client.close()
        assert stop_owner(holder) == 0
        assert not os.path.exists(live), "holder destroy left its own node behind"

        # A foreign (non-locking) live listener: create fails closed and the
        # node is untouched.
        raw = root / "raw.sock"
        rawproc, line = start_owner(owner, "holdraw", raw)
        assert line.startswith("READY "), line
        raw_ino = int(line.split()[1])
        result = run_owner_try(owner, raw)
        assert result.stdout.startswith("TRY NULL"), result.stdout
        assert os.stat(raw).st_ino == raw_ino, "foreign live node was replaced"
        assert rawproc.poll() is None, "foreign listener died"
        assert stop_owner(rawproc) == 0

        # A predecessor's destroy() must not unlink a successor's node.
        chain = root / "chain.sock"
        pred, line = start_owner(owner, "hold", chain)
        assert line.startswith("READY "), line
        pred_ino = int(line.split()[1])
        succ, line = start_owner(owner, "holdraw", chain)
        assert line.startswith("READY "), line
        succ_ino = int(line.split()[1])
        assert succ_ino != pred_ino, "successor did not replace the node"
        assert stop_owner(pred) == 0
        assert os.path.exists(chain), "predecessor destroy unlinked successor node"
        assert os.stat(chain).st_ino == succ_ino, "successor node replaced"
        assert succ.poll() is None, "successor listener died"
        assert stop_owner(succ) == 0

        # A stale socket node (bound then closed, no listener) is recovered.
        stale = root / "stale.sock"
        result = subprocess.run([str(owner), "mkstale", str(stale)],
                                capture_output=True, text=True, timeout=30)
        assert result.stdout.startswith("STALE"), result.stdout
        assert stat.S_ISSOCK(os.stat(stale).st_mode), "no stale node created"
        recovered, line = start_owner(owner, "hold", stale)
        assert line.startswith("READY "), line
        client = Client(str(stale))
        reply = client.request(TYPE_OPEN, open_payload(1))
        assert reply["type"] == TYPE_OPEN_ACK and reply["status"] == OK, reply
        client.close()
        assert stop_owner(recovered) == 0
        assert not os.path.exists(stale), "recovered holder left its node behind"

    print("audio_sink ownership: live collision, non-socket refusal, "
          "successor survival and stale recovery PASS")


def main():
    cc = os.environ.get("CC", "cc")
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="t", dir=str(FIXTURE_ROOT)) as temp:
        root = Path(temp)
        unit = compile_fixture(cc, "test_audio_sink_unit", UNIT_PROGRAM, root)
        server = compile_fixture(cc, "test_audio_sink_server", SERVER_PROGRAM, root)
        epoch_fail = compile_fixture(
            cc, "test_audio_sink_epoch_fail", EPOCH_FAIL_PROGRAM, root,
            extra_flags=("-DLE_AUDIO_SINK_TEST_FORCE_EPOCH_ENTROPY_FAILURE",))
        owner = compile_fixture(cc, "test_audio_sink_owner", OWNER_PROGRAM, root)

        subprocess.run([str(unit), str(root / "unit.sock")], check=True, timeout=60)
        run_ownership_suite(owner)
        subprocess.run([str(epoch_fail), str(root / "fail.sock")],
                       check=True, timeout=60)

        socket_path = root / "s.sock"
        process = subprocess.Popen(
            [str(server), str(socket_path), str(MAX_CAPACITY)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            wait_ready(process)
            run_socket_suite(server, socket_path)
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

        # Wire-level timing publication: an engine-side verified observation
        # for the live generation must appear in PROGRESS (TIMING_VALID plus
        # the finish horizon) without changing the payload layout, and must not
        # be reported as TIMING_INVALID.
        timing_finish = 987654321
        timing_socket = root / "timing.sock"
        timing_process = subprocess.Popen(
            [str(server), str(timing_socket), str(MAX_CAPACITY),
             str(timing_finish)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            wait_ready(timing_process)
            client = Client(str(timing_socket))
            reply = client.request(TYPE_OPEN, open_payload(1))
            assert reply["type"] == TYPE_OPEN_ACK and reply["status"] == OK, reply
            epoch = reply["epoch"]
            deadline = time.monotonic() + 3.0
            while True:
                reply = client.request(
                    TYPE_PROGRESS_REQ, progress_req_payload(epoch, 1))
                assert reply["type"] == TYPE_PROGRESS, reply
                if reply["pflags"] & FLAG_TIMING_VALID:
                    break
                if time.monotonic() > deadline:
                    raise AssertionError("timing never published: %r" % reply)
                time.sleep(0.01)
            assert reply["pflags"] & FLAG_TIMING_INVALID == 0, reply
            assert reply["finish_us"] == timing_finish, reply
        finally:
            timing_process.terminate()
            try:
                timing_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                timing_process.kill()
                timing_process.wait(timeout=3)
    print("test_audio_sink: compiled C sink ABI, bounded queue and socket PASS")


if __name__ == "__main__":
    main()

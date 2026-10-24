#!/usr/bin/env python3
"""Development Sendspin server for hot-staged LibreEcho players.

Connects to one or more players (server-initiated, no mDNS), trusts them for
unpaired playback (the players run with --dev-allow-unpaired), puts them in one
group and streams a test signal: a tone with a 5 ms click every second, so
grouped sync is audible. Run with the pinned aiosendspin.

  ss_probe_serve.py ws://HOST:8928/sendspin [...] [--seconds 30] [--level-db -20]
"""
import argparse
import asyncio
import logging
import math
import struct
import time

from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import InMemoryServerPairingStore
from aiosendspin.server import ClientAddedEvent, SendspinServer
from aiosendspin.server.audio import AudioFormat

RATE = 48000
CHUNK = 960  # 20 ms


def signal(start, frames, tone_hz, level):
    out = bytearray()
    amp = 32767 * level
    for n in range(start, start + frames):
        v = 0.6 * math.sin(2 * math.pi * tone_hz * n / RATE) if tone_hz else 0.0
        if n % RATE < RATE // 200:  # 5 ms click each second
            v = 1.0 if (n // 24) % 2 == 0 else -1.0
        s = int(amp * v)
        out += struct.pack('<hh', s, s)
    return bytes(out)


async def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('urls', nargs='+')
    p.add_argument('--seconds', type=float, default=30)
    p.add_argument('--tone', type=float, default=440, help='0 = clicks only')
    p.add_argument('--level-db', type=float, default=-20)
    p.add_argument('--connect-timeout', type=float, default=20)
    p.add_argument('--verbose', action='store_true')
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format='%(asctime)s %(name)s %(message)s')
    loop = asyncio.get_running_loop()
    server = SendspinServer(loop, Identity.generate(), 'LibreEcho probe',
                            pairing_store=InMemoryServerPairingStore())
    added = asyncio.Queue()

    def on_event(_server, event):
        if isinstance(event, ClientAddedEvent):
            added.put_nowait(event.client_id)

    server.add_event_listener(on_event)
    for url in a.urls:
        server.connect_to_client(url, retry_initial_connection=True)
    ids = []
    deadline = time.monotonic() + a.connect_timeout
    while len(ids) < len(a.urls):
        cid = await asyncio.wait_for(added.get(), max(0.1, deadline - time.monotonic()))
        await server.trust_unpaired(cid)
        ids.append(cid)
        print('client', cid[:12], 'trusted', flush=True)
    await asyncio.sleep(2)  # role activation + clock sync
    clients = [server.get_client(cid) for cid in ids]
    group = clients[0].group
    for c in clients[1:]:
        await group.add_client(c)
    print('group members', len(group.clients), flush=True)
    stream = group.start_stream()
    fmt = AudioFormat(sample_rate=RATE, bit_depth=16, channels=2)
    level = 10 ** (a.level_db / 20)
    sent = 0
    total = int(a.seconds * RATE)
    first_play = last_play = None
    t0 = time.monotonic()
    while sent < total:
        stream.prepare_audio(signal(sent, CHUNK, a.tone, level), fmt)
        last_play = await stream.commit_audio()
        first_play = first_play or last_play
        sent += CHUNK
    ahead = (last_play - server.clock.now_us()) / 1e6
    print(f'sent_frames {sent} in {time.monotonic() - t0:.2f}s, last chunk plays in {ahead:.2f}s',
          flush=True)
    # stream/end makes the player drop what it has not played yet: end only
    # once the last chunk's play time has passed.
    await asyncio.sleep(max(0.0, ahead) + CHUNK / RATE + 0.5)
    await group.stop()
    await asyncio.sleep(1)
    print('done', flush=True)


if __name__ == '__main__':
    asyncio.run(main())

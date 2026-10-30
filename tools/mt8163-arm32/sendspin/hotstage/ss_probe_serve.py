#!/usr/bin/env python3
"""Development Sendspin server for hot-staged LibreEcho players.

Connects to one or more players (server-initiated, no mDNS), trusts them for
unpaired playback (the players run with --dev-allow-unpaired), puts them in one
group and streams a test signal: a tone with a 5 ms click every second, so
grouped sync is audible. Run with the pinned aiosendspin.

  ss_probe_serve.py ws://HOST:8928/sendspin [...] [--seconds 30] [--level-db -20]
  ss_probe_serve.py --discover [--pairing-token-file FILE] ...

--discover browses _sendspin._tcp instead of taking URLs. With
--pairing-token-file the first player is paired with its Pairing PSK token
(the file is read, never printed) instead of being trusted unpaired.
"""
import argparse
import asyncio
import logging
import math
import struct
import time

from aiohttp import web
from aiohttp.test_utils import TestServer

from aiosendspin.client import SendspinClient as VirtualClient
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PairMethod, Roles
from aiosendspin.noise.pairing import PairingAttempt
from aiosendspin.noise.pairing_token import decode_psk_token
from aiosendspin.noise.keys import Identity, generate_psk, psk_id_for
from aiosendspin.noise.trust_store import (
    ClientPairingRecord,
    InMemoryClientPairingStore,
    InMemoryServerPairingStore,
    ServerPairingRecord,
)
from aiosendspin.server import ClientAddedEvent, SendspinServer
from aiosendspin.server.audio import AudioFormat
from aiosendspin.server.roles.player.v1 import PlayerV1Role

RATE = 48000
CHUNK = 960  # 20 ms


async def start_virtual_players(server, server_store, count):
    """Host-side players (no audio output) that join the group as members."""
    app = web.Application()
    app.router.add_get(SendspinServer.API_PATH, server.on_client_connect)
    listener = TestServer(app)
    await listener.start_server()
    url = f'ws://127.0.0.1:{listener.port}{SendspinServer.API_PATH}'
    clients, received = [], []
    for i in range(count):
        identity, store, psk = Identity.generate(), InMemoryClientPairingStore(), generate_psk()
        await server_store.store_record(ServerPairingRecord(
            psk_id=psk_id_for(psk), psk=psk, client_id=identity.peer_id, pair_methods=[]))
        await store.store_record(ClientPairingRecord(
            psk_id=psk_id_for(psk), psk=psk, server_id=server.id))
        client = VirtualClient(
            identity=identity, pairing_store=store, client_name=f'virtual-{i}',
            roles=[Roles.PLAYER],
            player_support=ClientHelloPlayerSupport(
                supported_formats=[SupportedAudioFormat(
                    codec=AudioCodec.PCM, channels=2, sample_rate=RATE, bit_depth=16)],
                buffer_capacity=1_000_000, supported_commands=[]))
        frames = [0]
        client.add_audio_chunk_listener(
            lambda *args, frames=frames: frames.__setitem__(0, frames[0] + 1))
        await client.connect(url)
        clients.append(client)
        received.append(frames)
    return listener, clients, received


async def discover(seconds=4.0):
    """Browse _sendspin._tcp; print and return each player's WebSocket URL."""
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
    found = {}
    zc = AsyncZeroconf()

    def on_change(zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added:
            found[name] = service_type

    browser = AsyncServiceBrowser(zc.zeroconf, '_sendspin._tcp.local.', handlers=[on_change])
    await asyncio.sleep(seconds)
    urls = []
    for name, service_type in found.items():
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(zc.zeroconf, 3000):
            continue
        txt = {k.decode(): (v or b'').decode() for k, v in info.properties.items()}
        address = info.parsed_addresses()[0]
        print(f'discovered {name!r} port={info.port} txt={txt}', flush=True)
        urls.append(f'ws://{address}:{info.port}{txt.get("path", "/sendspin")}')
    await browser.async_cancel()
    await zc.async_close()
    return urls


def player_role(client):
    return next((r for r in client.active_roles if isinstance(r, PlayerV1Role)), None)


async def run_script(client, script, group, ctl, clock):
    """Timed commands: volume/mute to one player, leave/join of that player,
    and clear (a seek: pending audio dropped, timing reset).  Each line is
    stamped with the server clock."""
    start = time.monotonic()
    for step in filter(None, script.split(',')):
        at, _, action = step.partition(':')
        await asyncio.sleep(max(0.0, start + float(at) - time.monotonic()))
        now = clock.now_us()
        if action == 'leave':
            await group.remove_client(client)
            print(f'script t={at}s leave at_us={now}', flush=True)
            continue
        if action == 'join':
            await group.add_client(client)
            print(f'script t={at}s join at_us={now}', flush=True)
            continue
        if action == 'clear':
            ctl['stream'].clear()
            ctl['cleared'] = True
            print(f'script t={at}s clear at_us={now}', flush=True)
            continue
        role = player_role(client)
        if role is None:
            print('script: no player role', flush=True)
            return
        if action.startswith('volume='):
            role.set_player_volume(int(action.split('=', 1)[1]))
        elif action in ('mute', 'unmute'):
            role.set_player_mute(action == 'mute')
        await asyncio.sleep(0.5)
        print(f'script t={at}s {action}: player reports volume={role.volume} '
              f'muted={role.muted}', flush=True)


def signal(start, frames, tone_hz, level, click_frames=RATE // 200):
    out = bytearray()
    amp = 32767 * level
    for n in range(start, start + frames):
        v = 0.6 * math.sin(2 * math.pi * tone_hz * n / RATE) if tone_hz else 0.0
        if n % RATE < click_frames:  # click each second (5 ms default)
            v = 1.0 if (n // 24) % 2 == 0 else -1.0
        s = int(amp * v)
        out += struct.pack('<hh', s, s)
    return bytes(out)


async def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('urls', nargs='*')
    p.add_argument('--discover', action='store_true',
                   help='find players by browsing _sendspin._tcp for a few seconds')
    p.add_argument('--pairing-token-file', help='pair the first player with this token')
    p.add_argument('--seconds', type=float, default=30)
    p.add_argument('--tone', type=float, default=440, help='0 = clicks only')
    p.add_argument('--level-db', type=float, default=-20)
    p.add_argument('--click-ms', type=float, default=5,
                   help='click length; 0.5 keeps near and far arrivals apart')
    p.add_argument('--connect-timeout', type=float, default=20)
    p.add_argument('--virtual-players', type=int, default=0,
                   help='also group N host-side players without audio output')
    p.add_argument('--script', default='',
                   help="player commands at seconds after stream start, e.g. "
                        "'3:volume=40,6:mute,9:unmute' (one player, see --script-player)")
    p.add_argument('--script-player', type=int, default=0,
                   help='index of the scripted player, in command-line order')
    p.add_argument('--lead', type=float, default=0,
                   help='keep at most this many seconds of audio ahead (0: send all at once)')
    p.add_argument('--verbose', action='store_true')
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format='%(asctime)s %(name)s %(message)s')
    loop = asyncio.get_running_loop()
    server_store = InMemoryServerPairingStore()
    server = SendspinServer(loop, Identity.generate(), 'LibreEcho probe',
                            pairing_store=server_store)
    added = asyncio.Queue()

    def on_event(_server, event):
        if isinstance(event, ClientAddedEvent):
            added.put_nowait(event.client_id)

    server.add_event_listener(on_event)
    urls = list(a.urls)
    if a.discover:
        urls += await discover()
    if not urls:
        raise SystemExit('no players')
    attempt = None
    if a.pairing_token_file:
        token = decode_psk_token(open(a.pairing_token_file).read().strip())
        attempt = PairingAttempt(method=PairMethod.PAIRING_PSK, pairing_psk=token.pairing_psk,
                                 client_id=token.client_id)
    # One at a time, so the client order matches the command line.
    ids = []
    deadline = time.monotonic() + a.connect_timeout
    for index, url in enumerate(urls):
        server.connect_to_client(url, retry_initial_connection=True,
                                 pairing_attempt=attempt if index == 0 else None)
        cid = await asyncio.wait_for(added.get(), max(0.1, deadline - time.monotonic()))
        if attempt is None or index > 0:
            await server.trust_unpaired(cid)
        ids.append(cid)
        print('client', index, cid[:12],
              'paired by token' if attempt and index == 0 else 'trusted', flush=True)
    virtual = None
    if a.virtual_players:
        virtual = await start_virtual_players(server, server_store, a.virtual_players)
        for vc in virtual[1]:
            ids.append(vc.identity.peer_id)
    await asyncio.sleep(2)  # role activation + clock sync
    clients = [server.get_client(cid) for cid in ids]
    group = clients[0].group
    for c in clients[1:]:
        await group.add_client(c)
    print('group members', len(group.clients), flush=True)
    print('stream_start_us', server.clock.now_us(), flush=True)
    stream = group.start_stream()
    ctl = {'stream': stream, 'cleared': False}
    script_task = asyncio.create_task(run_script(clients[a.script_player], a.script, group, ctl,
                                                 server.clock)) if a.script else None
    fmt = AudioFormat(sample_rate=RATE, bit_depth=16, channels=2)
    level = 10 ** (a.level_db / 20)
    sent = 0
    total = int(a.seconds * RATE)
    first_play = last_play = None
    t0 = time.monotonic()
    while sent < total:
        stream.prepare_audio(signal(sent, CHUNK, a.tone, level, max(1, int(a.click_ms * RATE / 1000))), fmt)
        cleared = ctl['cleared']
        ctl['cleared'] = False
        last_play = await stream.commit_audio()
        if first_play is None:
            first_play = last_play
            # Server clock time of stream frame 0; click k plays at this + k seconds.
            print('stream_first_play_us', first_play, flush=True)
        if cleared:
            # Frame `sent` now plays at last_play; clicks stay on n % RATE.
            print(f'stream_restart_frame {sent} play_us {last_play}', flush=True)
        sent += CHUNK
        if a.lead and last_play - server.clock.now_us() > a.lead * 1e6:
            await asyncio.sleep((last_play - server.clock.now_us()) / 1e6 - a.lead)
    ahead = (last_play - server.clock.now_us()) / 1e6
    print(f'sent_frames {sent} in {time.monotonic() - t0:.2f}s, last chunk plays in {ahead:.2f}s',
          flush=True)
    # stream/end makes the player drop what it has not played yet: end only
    # once the last chunk's play time has passed.
    await asyncio.sleep(max(0.0, ahead) + CHUNK / RATE + 0.5)
    if script_task:
        await script_task
    role = player_role(clients[0])
    if role is not None:
        print(f'player reports volume={role.volume} muted={role.muted}', flush=True)
    await group.stop()
    await asyncio.sleep(1)
    if virtual:
        print('virtual players received chunks', [r[0] for r in virtual[2]], flush=True)
        for vc in virtual[1]:
            await vc.disconnect()
        await virtual[0].close()
    print('done', flush=True)


if __name__ == '__main__':
    asyncio.run(main())

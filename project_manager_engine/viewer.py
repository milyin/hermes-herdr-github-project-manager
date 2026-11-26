"""Launch the native Hermes TUI and attest its live gateway attachment."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import struct
import sys
from contextlib import suppress

from .hermes_native import address, process_state


def viewer_state(pid):
    """The Hermes launcher and its foreground renderer can stop independently."""
    pending = [pid]
    state = process_state(pid)
    while pending and len(pending) < 100:
        current = pending.pop()
        if process_state(current) in ('T', 't'):
            return 'T'
        try:
            children = Path(f'/proc/{current}/task/{current}/children').read_text()
        except OSError:
            continue
        pending.extend(map(int, children.split()))
    return state


async def launch(home, workspace, expected):
    from hermes_cli.gateway_client import gateway_ws_target
    from hermes_cli.gateway_runtime_attach import attach_gateway
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
    from websockets.exceptions import ConnectionClosed

    pane = os.environ.get('HERDR_PANE_ID')
    if not pane:
        raise RuntimeError('The managed TUI must run in its Herdr pane')
    hermes = shutil.which('hermes')
    if hermes is None:
        raise RuntimeError('The Herdr server cannot find Hermes on PATH')
    token = '/' + secrets.token_urlsafe(32)
    attached = None
    child = None
    live_connection = None

    async def status(reader, writer):
        peer = writer.get_extra_info('socket')
        try:
            _pid, uid, _gid = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid == os.getuid():
                child_status = viewer_state(child.pid) if child else None
                row = dict(pane_id=pane, workspace_id=workspace, session_id=attached,
                           suspended=child_status in ('T', 't'))
                if child_status in (None, 'Z', 'X'):
                    row['session_id'] = None
                writer.write((json.dumps(row) + '\n').encode())
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def relay(viewer):
        nonlocal attached, live_connection
        if viewer.request.path != token or live_connection is not None:
            await viewer.close(1008, 'This endpoint belongs to the bound manager pane')
            return
        live_connection = viewer
        waiting = {}

        async def send_upstream(owner):
            nonlocal attached
            async for wire in viewer:
                frame = json.loads(wire)
                method = frame.get('method')
                if method in ('session.resume', 'session.create', 'session.detach', 'session.branch'):
                    attached = None
                    if 'id' in frame:
                        waiting[frame['id']] = method
                await owner.send(wire)

        async def send_downstream(owner):
            nonlocal attached
            async for wire in owner:
                frame = json.loads(wire)
                method = waiting.pop(frame.get('id'), None)
                if method in ('session.resume', 'session.create', 'session.branch'):
                    session_id = (frame.get('result') or {}).get('session_id')
                    if isinstance(session_id, str):
                        attached = session_id
                await viewer.send(wire)

        try:
            result = await asyncio.to_thread(attach_gateway, home, start=False, timeout=10)
            if result.ticket is None:
                await viewer.close(1011, 'Local Hermes gateway is unavailable')
                return
            upstream, protocols = gateway_ws_target(result.receipt.endpoint, result.ticket)
            async with connect(upstream, subprotocols=protocols, proxy=None, max_size=8 * 1024 * 1024) as owner:
                if owner.subprotocol != protocols[0]:
                    await viewer.close(1011, 'Local Hermes gateway protocol changed')
                    return
                await viewer.send(json.dumps({
                    'jsonrpc': '2.0', 'method': 'event',
                    'params': {'type': 'gateway.ready', 'session_id': '',
                               'payload': {'session_authority': True}}}))
                tasks = [asyncio.create_task(send_upstream(owner)), asyncio.create_task(send_downstream(owner))]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (ValueError, OSError, ConnectionClosed):
            pass
        finally:
            attached = None
            live_connection = None
            await viewer.close()

    async with await asyncio.start_unix_server(status, path=address(home, workspace, pane)):
        async with serve(relay, '127.0.0.1', 0, max_size=8 * 1024 * 1024) as server:
            port = server.sockets[0].getsockname()[1]
            env = dict(os.environ, HERMES_HOME=str(home), HERMES_TUI_GATEWAY_URL=f'ws://127.0.0.1:{port}{token}',
                       HERMES_TUI_NATIVE='0', HERMES_NONINTERACTIVE='1')
            for name in ('HERMES_PROFILE', 'HERMES_PROFILE_NAME', 'HERMES_TUI_SIDECAR_URL'):
                env.pop(name, None)
            child = await asyncio.create_subprocess_exec(hermes, '--tui', '--resume', expected, env=env)
            try:
                return await child.wait()
            finally:
                if child.returncode is None:
                    with suppress(ProcessLookupError):
                        child.terminate()
                    await child.wait()


if __name__ == '__main__':
    try:
        raise SystemExit(asyncio.run(launch(Path(sys.argv[1]), sys.argv[2], sys.argv[3])))
    except (OSError, RuntimeError) as exc:
        print(f'[project-manager] {exc}', file=sys.stderr)
        raise SystemExit(1)

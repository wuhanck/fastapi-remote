#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import asyncio
import asyncssh
import json
import os
import logging
import sys
import traceback
import re

from fastapi import FastAPI
from fastapi.websockets import WebSocket, WebSocketDisconnect, WebSocketState
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = FastAPI()
app.mount('/node_modules', StaticFiles(directory='node_modules'), name='node_modules')
app.mount('/static', StaticFiles(directory='static'), name='static')

DEFAULT_SSH_USER = os.environ.get('WS_SSH_USER', 'cc')
DEFAULT_SSH_PASS = os.environ.get('WS_SSH_PASS', 'cc')
DEFAULT_VNC_USER = ''
DEFAULT_VNC_PASS = ''


def fmt_e(exc: Exception, indent: int=0) -> str:
    prefix = ' ' * indent
    summary = traceback.extract_tb(exc.__traceback__, limit=1)[0]
    file_info = f'file: {summary.filename}'
    line_info = f'line: {summary.lineno}'
    type_info = type(exc).__name__
    message = str(exc)

    main_info = f'{prefix}* {type_info} ({file_info} {line_info}): {message}'

    output_lines = [main_info]

    if isinstance(exc, ExceptionGroup):
        for nested_exc in exc.exceptions:
            output_lines.append(fmt_e(nested_exc, indent + 4))

    return '\n'.join(output_lines)


def parse_x_path(x_path, d_user, d_pass, d_port):
    '''
    user@host:port@passwd. user/port/passwd printable
    '''
    m = re.match(r'^(?:(?P<user>[^@/]+)@)?(?P<host>[^@:]+)(?::(?P<port>\d+))?(?:@(?P<passwd>[^@]+))?$', x_path)
    if not m:
        raise ValueError('parse-x-path [user@]host[:port][@passwd]')
    user = m.group('user') or d_user
    host = m.group('host')
    port = int(m.group('port') or d_port)
    passwd = m.group('passwd') or d_pass
    return user, host, port, passwd


@app.get('/ssh/{ssh_path:path}')
async def ssh_page(ssh_path: str):
    return FileResponse('static/ssh.html')


async def ssh_shell(ws: WebSocket, user: str, host: str, port: int, passwd: str, init_cols: int=80, init_rows: int=24):
    async with asyncssh.connect(host, port=port, username=user, password=passwd, known_hosts=None) as conn:
        async with conn.create_process(term_type='xterm', term_size=(init_cols, init_rows), stderr=asyncssh.STDOUT) as process:

            async def ws_to_ssh():
                while True:
                    raw = await ws.receive()
                    if raw.get('text'):
                        data = raw['text']
                        process.stdin.write(data)
                        await process.stdin.drain()
                    elif raw.get('bytes'):
                        data = raw['bytes']
                        msg = json.loads(data.decode('utf-8'))
                        if "resize" in msg:
                            cols = int(msg['resize'].get('cols', init_cols))
                            rows = int(msg['resize'].get('rows', init_rows))
                            process.change_terminal_size(cols, rows)
                        logging.info(f'ws-to-ssh ctl {msg}')
                    elif raw['type'] == 'websocket.disconnect':
                        raise UserWarning('ws-to-ssh ws disconnect')

            async def ssh_to_ws():
                while True:
                    data = await process.stdout.read(4096)
                    if not data:
                        raise UserWarning('ssh-to-ws process close')
                    await ws.send_text(data)

            async with asyncio.TaskGroup() as tg:
                tg.create_task(ws_to_ssh())
                tg.create_task(ssh_to_ws())


@app.websocket('/ws-ssh/')
async def ws_ssh(websocket: WebSocket):
    await websocket.accept()
    logging.debug('ws-ssh start')

    try:
        ssh_path = websocket.query_params.get('ssh')
        init_cols = int(websocket.query_params.get('cols') or 80)
        init_rows = int(websocket.query_params.get('rows') or 24)
        if not ssh_path:
            raise UserWarning('ws-ssh missing ssh path')

        user, host, port, passwd = parse_x_path(ssh_path, DEFAULT_SSH_USER, DEFAULT_SSH_PASS, 22)
        await ssh_shell(websocket, user, host, port, passwd, init_cols, init_rows)
    except Exception as e:
        logging.info(f'ws-ssh\n{fmt_e(e)}')

    if websocket.client_state != WebSocketState.DISCONNECTED:
        await websocket.close()
    logging.debug('ws-ssh end')


@app.get('/vnc/{vnc_path:path}')
async def vnc_page(vnc_path: str):
    return FileResponse('static/vnc.html')


async def vnc_relay(websocket, user, host, port, passwd):
    tcp_reader, tcp_writer = await asyncio.open_connection(host, port)

    async def ws_to_tcp():
        while True:
            data = await websocket.receive_bytes()
            tcp_writer.write(data)
            await tcp_writer.drain()

    async def tcp_to_ws():
        while True:
            data = await tcp_reader.read(4096)
            if data == b'':
                await asyncio.sleep(15)  # wait for client to gracefully end session
                raise UserWarning('vnc-relay can close')
            await websocket.send_bytes(data)

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(ws_to_tcp())
            tg.create_task(tcp_to_ws())
    finally:
            tcp_writer.close()
            await tcp_writer.wait_closed()


@app.websocket('/ws-vnc/')
async def ws_vnc(websocket: WebSocket):
    await websocket.accept()
    logging.debug('ws-vnc start')

    try:
        vnc_path = websocket.query_params.get('vnc')
        if not vnc_path:
            raise UserWarning('ws-vnc missing vnc path')

        user, host, port, passwd = parse_x_path(vnc_path, DEFAULT_VNC_USER, DEFAULT_VNC_PASS, 5900)
        await vnc_relay(websocket, user, host, port, passwd)
    except Exception as e:
        logging.info(f'ws-vnc\n{fmt_e(e)}')

    if websocket.client_state != WebSocketState.DISCONNECTED:
        await websocket.close()
    logging.debug('ws-vnc end')


if __name__ == '__main__':
    import uvicorn
    logger.info(f'Attempting to start Uvicorn from __main__...')
    #uvicorn.run('main:app', host='0.0.0.0', port=8080, reload=True, log_level='debug')
    uvicorn.run('main:app', host='0.0.0.0', port=8080, reload=True, log_level='info')

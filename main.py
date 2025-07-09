#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import asyncio
import asyncssh
import json
import os
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


app = FastAPI()
app.mount('/node_modules', StaticFiles(directory='node_modules'), name='node_modules')
app.mount('/static', StaticFiles(directory='static'), name='static')

# 获取用户名密码环境变量，若无则默认'cc'
DEFAULT_SSH_USER = os.environ.get('WS_SSH_USER', 'cc')
DEFAULT_SSH_PASS = os.environ.get('WS_SSH_PASS', 'cc')


@app.get('/ssh/{ssh_path:path}')
async def ssh_page(ssh_path: str):
    return FileResponse('static/index.html')


def parse_ssh_path(ssh_path):
    '''
    解析 user@host:port@passwd 格式，user/port/passwd均可缺省。passwd明文。
    '''
    import re
    m = re.match(r'^(?:(?P<user>[^@/]+)@)?(?P<host>[^@:]+)(?::(?P<port>\d+))?(?:@(?P<passwd>[^@]+))?$', ssh_path)
    if not m:
        raise ValueError('地址格式需为 [user@]host[:port][@passwd]')
    user = m.group('user') or DEFAULT_SSH_USER
    host = m.group('host')
    port = int(m.group('port') or 22)
    passwd = m.group('passwd') or DEFAULT_SSH_PASS
    return user, host, port, passwd


async def ssh_shell(ws: WebSocket, user: str, host: str, port: int, passwd: str, init_cols: int = 80, init_rows: int = 24):
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
                        logging.info(f'ws ctl {msg}')
                    elif raw['type'] == 'websocket.disconnect':
                        logging.info(f'ws disconnect')
                        break

            async def ssh_to_ws():
                while True:
                    data = await process.stdout.read(4096)
                    if not data:
                        logging.info(f'ssh process close')
                        break
                    await ws.send_text(data)

            await asyncio.gather(ws_to_ssh(), ssh_to_ws())


@app.websocket("/ws/")
async def ssh_ws(websocket: WebSocket):
    await websocket.accept()
    ssh_path = websocket.query_params.get('ssh')
    init_cols = int(websocket.query_params.get('cols') or 80)
    init_rows = int(websocket.query_params.get('rows') or 24)
    if not ssh_path:
        await websocket.send_text('缺少 ssh 参数，例如 cc@host:22@passwd')
        await websocket.close()
        return

    try:
        user, host, port, passwd = parse_ssh_path(ssh_path)
    except Exception as e:
        await websocket.send_text(str(e))
        await websocket.close()
        return

    try:
        await ssh_shell(websocket, user, host, port, passwd, init_cols, init_rows)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        await websocket.send_text(f"[异常]{str(e)}")
        await websocket.close()


if __name__ == '__main__':
    import uvicorn
    logger.info(f'Attempting to start Uvicorn from __main__...')
    uvicorn.run('main:app', host='0.0.0.0', port=8080, reload=True, log_level='info')

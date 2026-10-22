"""Web send page (TCP 8892), mirroring the official Flix web client [§6].

- GET /            PIN entry page (4 digit inputs, auto submit)
- POST /validate-code {"code":"1234"} -> 200 / 429 / 401
- GET /<pin>       send page: WebSocket /ws (text), POST /upload (files)
- WS  /ws?pin=...  plaintext text messages; server pushes "refresh" or text

The PIN rotates on every daemon start (stored in prefs, printed at startup).
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Callable

from aiohttp import WSMsgType, web

PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>Flix 网页版</title>
<style>
body{font-family:system-ui;background:#111;color:#eee;margin:0;display:flex;
flex-direction:column;height:100vh}
.chat{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:8px}
.bubble{max-width:70%;padding:8px 12px;border-radius:10px;word-break:break-all}
.left{background:#2a2a2a;align-self:flex-start}
.right{background:#0a84ff;align-self:flex-end}
.footer{display:flex;gap:8px;padding:12px;border-top:1px solid #333}
.footer input[type=text]{flex:1;padding:10px;border-radius:5px;border:1px solid #ccc;
background:#1c1c1c;color:#eee;font-size:16px}
button{padding:10px 20px;border:none;border-radius:5px;background:#007bff;
color:#fff;font-size:15px;cursor:pointer}
</style></head><body>
<div class="chat" id="chat"></div>
<div class="footer">
<input type="text" id="msg" placeholder="发送到本机…" autofocus>
<input type="file" id="file" multiple style="display:none">
<button onclick="document.getElementById('file').click()">文件</button>
<button id="send">发送</button>
</div>
<script>
const ws = new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');
const chat = document.getElementById('chat');
function add(t, right){const d=document.createElement('div');d.className='bubble '+(right?'right':'left');
d.textContent=t;chat.appendChild(d);chat.scrollTop=chat.scrollHeight;}
ws.onmessage = e => { if (e.data === 'refresh') location.reload(); else add(e.data, false); };
function sendText(){const i=document.getElementById('msg');const v=i.value.trim();
if(!v)return;ws.send(v);add(v,true);i.value='';}
document.getElementById('send').onclick = sendText;
document.getElementById('msg').addEventListener('keydown', e=>{if(e.key==='Enter')sendText();});
document.getElementById('file').addEventListener('change', async e=>{
  for (const f of e.target.files) {
    const fd = new FormData(); fd.append('file', f);
    const r = await fetch('/upload', {method:'POST', body: fd});
    add(f.name + (r.ok ? ' ✓' : ' ✗'), true);
  }
  e.target.value='';
});
</script></body></html>"""

INDEX = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>Flix 网页版</title>
<style>body{font-family:system-ui;background:#111;color:#eee;display:flex;
flex-direction:column;align-items:center;justify-content:center;height:100vh;margin:0}
h1{margin-bottom:24px}.row{display:flex;gap:10px}
input{width:64px;height:76px;font-size:40px;text-align:center;border-radius:8px;
border:1px solid #444;background:#1c1c1c;color:#eee}
button{margin-top:28px;padding:12px 48px;border:none;border-radius:8px;
background:#007bff;color:#fff;font-size:18px;cursor:pointer}</style></head>
<body onload="document.getElementById('i0').focus()">
<h1>输入连接码</h1>
<form onsubmit="return go(event)"><div class="row">
""" + "".join(
    f'<input id="i{i}" type="text" inputmode="numeric" maxlength="1" '
    f'oninput="next({i})" onkeydown="back(event,{i})">' for i in range(4)
) + """</div><button type="submit">连接</button></form>
<p id="err" style="color:#f66"></p>
<script>
function next(i){const e=document.getElementById('i'+i);
if(e.value.length===1&&i<3)document.getElementById('i'+(i+1)).focus();
if([...document.querySelectorAll('input')].every(x=>x.value))
document.querySelector('form').requestSubmit();}
function back(e,i){if(e.key==='Backspace'&&!e.target.value&&i>0)
document.getElementById('i'+(i-1)).focus();}
async function go(ev){ev.preventDefault();
const code=[...document.querySelectorAll('input')].map(x=>x.value).join('');
const r=await fetch('/validate-code',{method:'POST',
headers:{'Content-Type':'application/json'},body:JSON.stringify({code})});
if(r.ok){location.href='/'+code;}
else if(r.status===429){document.getElementById('err').textContent='错误过多，请30分钟后再试';}
else{document.getElementById('err').textContent='连接码错误，请重新输入';}
return false;}
</script></body></html>"""


def make_web_app(store, ident: dict, on_web_text: Callable[[str], None],
                 save_dir_fn, channel_port: int) -> tuple[web.Application, Callable[[str], None]]:
    """Web app + push_text(ws_text) callback for pushing text into open sockets.

    The page is meant for OTHER devices: a visitor enters the PIN, then their
    text lands in our store via on_web_text and their files in save_dir_fn().
    """
    from . import DEVICE_PORT

    sockets: set[web.WebSocketResponse] = set()

    def push_text(text: str) -> None:
        for ws in list(sockets):
            asyncio.ensure_future(ws.send_str(text))

    def pin() -> str:
        return store.own_web_pin()

    async def index(request: web.Request) -> web.Response:
        return web.Response(text=INDEX, content_type="text/html", charset="utf-8")

    async def validate(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.Response(status=401, text="bad request")
        fails = int(request.app["fails"].get(request.remote, 0))
        if fails >= 10:
            return web.Response(status=429, text="too many attempts")
        if payload.get("code") == pin():
            request.app["fails"].pop(request.remote, None)
            return web.Response(text="ok")
        request.app["fails"][request.remote] = fails + 1
        return web.Response(status=401, text="wrong code")

    def authorized(request: web.Request) -> bool:
        return request.match_info.get("pin") == pin() or \
            request.query.get("pin") == pin()

    async def app_page(request: web.Request) -> web.Response:
        if not authorized(request):
            raise web.HTTPFound("/")
        return web.Response(text=PAGE, content_type="text/html", charset="utf-8")

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        if not authorized(request):
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sockets.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    text = msg.data.strip()
                    if text:
                        on_web_text(text)
        finally:
            sockets.discard(ws)
        return ws

    async def upload(request: web.Request) -> web.Response:
        if not authorized(request):
            return web.Response(status=401, text="unauthorized")
        reader = await request.multipart()
        saved = []
        while part := await reader.next():
            if part.name != "file":
                continue
            dest_dir = Path(save_dir_fn())
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / part.filename
            tmp = dest_dir / (part.filename + ".flixdownload")
            size = 0
            with open(tmp, "wb") as f:
                while chunk := await part.read_chunk(256 * 1024):
                    f.write(chunk)
                    size += len(chunk)
            import os
            os.replace(tmp, dest)
            saved.append({"name": part.filename, "size": size, "path": str(dest)})
        return web.json_response({"ok": True, "saved": saved})

    app = web.Application()
    app["fails"] = {}
    app.router.add_get("/", index)
    app.router.add_post("/validate-code", validate)
    app.router.add_get("/{pin}", app_page)
    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/upload", upload)
    return app, push_text

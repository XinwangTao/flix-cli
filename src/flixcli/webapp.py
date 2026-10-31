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
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flix 网页版</title>
<style>
body{font-family:system-ui;background:#111;color:#eee;margin:0;display:flex;
flex-direction:column;height:100vh}
.chat{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:8px}
.bubble{max-width:70%;padding:8px 12px;border-radius:10px;word-break:break-all}
.left{background:#2a2a2a;align-self:flex-start}
.right{background:#0a84ff;align-self:flex-end}
.meta{font-size:11px;opacity:.6;margin-bottom:2px}
.footer{display:flex;gap:8px;padding:12px;border-top:1px solid #333;flex-wrap:wrap}
.footer input[type=text]{flex:1;min-width:140px;padding:10px;border-radius:5px;
border:1px solid #ccc;background:#1c1c1c;color:#eee;font-size:16px}
select,input[type=file]{padding:8px;border-radius:5px;background:#1c1c1c;color:#eee}
button{padding:10px 20px;border:none;border-radius:5px;background:#007bff;
color:#fff;font-size:15px;cursor:pointer}
</style></head><body>
<div class="chat" id="chat"></div>
<div class="footer">
<select id="dev"><option value="">本机</option></select>
<input type="text" id="msg" placeholder="消息…（选设备=发给该设备）" autofocus>
<input type="file" id="file" multiple style="display:none">
<button onclick="document.getElementById('file').click()">文件</button>
<button id="send">发送</button>
</div>
<p style="text-align:center;margin:6px 0"><a id="glink" href="#"
style="color:#7ab8ff">群聊视图 →</a></p>
<script>
document.getElementById('glink').href =
  '/group?pin=' + location.pathname.slice(1);
const PIN = location.pathname.slice(1);
fetch('/devices?pin=' + PIN).then(r => r.json()).then(j => {
  const sel = document.getElementById('dev');
  for (const d of j.devices) {
    const o = document.createElement('option');
    o.value = d.fingerprint; o.textContent = d.alias || d.fingerprint.slice(0, 8);
    sel.appendChild(o);
  }
});
const ws = new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');
const chat = document.getElementById('chat');
function add(t, right, meta){
  const d=document.createElement('div');d.className='bubble '+(right?'right':'left');
  if(meta){const m=document.createElement('div');m.className='meta';m.textContent=meta;
    d.appendChild(m);}
  d.appendChild(document.createTextNode(t));chat.appendChild(d);chat.scrollTop=chat.scrollHeight;}
ws.onmessage = e => { if (e.data === 'refresh') location.reload(); else add(e.data, false); };
async function sendText(){
  const i=document.getElementById('msg');const v=i.value.trim();if(!v)return;
  const to=document.getElementById('dev').value;
  if(!to){ws.send(v);add(v,true,'本机');i.value='';return;}
  const r=await fetch('/send',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pin:PIN,to,text:v})});
  const j=await r.json().catch(()=>({}));
  add(v,true,(j.ok?'已送达 ':'失败 ')+JSON.stringify(j).slice(0,60));i.value='';
}
document.getElementById('send').onclick = sendText;
document.getElementById('msg').addEventListener('keydown', e=>{if(e.key==='Enter')sendText();});
document.getElementById('file').addEventListener('change', async e=>{
  const to=document.getElementById('dev').value;
  for (const f of e.target.files) {
    const fd = new FormData(); fd.append('file', f); fd.append('pin', PIN);
    if (to) fd.append('to', to);
    const r = await fetch('/upload?pin=' + PIN, {method:'POST', body: fd});
    const j = await r.json().catch(()=>({}));
    add(f.name + (r.ok ? ' ✓' : ' ✗') + (to && j.ok ? ' → 已转发设备' : ''), !!to,
        to ? '发往所选设备' : '本机');
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


GROUP_LIST = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>Flix 群聊</title>
<style>body{font-family:system-ui;background:#111;color:#eee;max-width:720px;
margin:0 auto;padding:24px}a{color:#7ab8ff;text-decoration:none;font-size:18px;
display:block;padding:10px 14px;background:#1c1c1c;border-radius:8px;margin:8px 0}
</style></head><body><h2>群聊视图</h2><div id="gs">加载中…</div>
<script>
const PIN = new URLSearchParams(location.search).get('pin');
fetch('/group/json?pin=' + PIN).then(r => r.json()).then(j => {
  const gs = document.getElementById('gs');
  gs.innerHTML = j.groups.map(g =>
    `<a href="/group/${g.id}?pin=${PIN}">${g.name} <small>(${g.members} 成员)</small></a>`
  ).join('') || '(无群组)';
});
</script></body></html>"""

GROUP_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flix 群聊</title>
<style>
body{font-family:system-ui;background:#111;color:#eee;margin:0;display:flex;
flex-direction:column;height:100vh}
.chat{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:8px}
.bubble{max-width:75%;padding:8px 12px;border-radius:10px;word-break:break-all}
.left{background:#2a2a2a;align-self:flex-start}
.right{background:#0a84ff;align-self:flex-end}
.meta{font-size:11px;opacity:.6;margin-bottom:2px}
.footer{display:flex;gap:8px;padding:12px;border-top:1px solid #333}
.footer input[type=text]{flex:1;padding:10px;border-radius:5px;border:1px solid #ccc;
background:#1c1c1c;color:#eee;font-size:16px}
button{padding:10px 20px;border:none;border-radius:5px;background:#007bff;
color:#fff;font-size:15px;cursor:pointer}
</style></head><body>
<div class="chat" id="chat"></div>
<div class="footer">
<input type="text" id="gmsg" placeholder="发言到群…" autofocus>
<button id="gsend">发送</button>
</div>
<script>
const PIN = new URLSearchParams(location.search).get('pin');
const GID = location.pathname.split('/')[2];
const chat = document.getElementById('chat');
let after = 0;
function add(who, text, self, tm){
  const d = document.createElement('div');
  d.className = 'bubble ' + (self ? 'right' : 'left');
  const m = document.createElement('div'); m.className = 'meta';
  m.textContent = who + ' · ' + new Date(tm).toLocaleTimeString();
  d.appendChild(m); d.appendChild(document.createTextNode(text));
  chat.appendChild(d); chat.scrollTop = chat.scrollHeight;
}
async function poll(){
  try {
    const r = await fetch(`/group/${GID}/data?pin=${PIN}&after=${after}`);
    if (!r.ok) return;
    const j = await r.json();
    for (const it of j.items){
      add(it.who, it.text, it.self, it.time);
      after = Math.max(after, it.time);
    }
  } catch (e) {}
}
async function gsend(){
  const i = document.getElementById('gmsg'); const v = i.value.trim();
  if (!v) return;
  await fetch('/send', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({pin: PIN, to: 'group:' + GID, text: v})});
  i.value = ''; setTimeout(poll, 800);
}
document.getElementById('gsend').onclick = gsend;
document.getElementById('gmsg').addEventListener('keydown',
  e => { if (e.key === 'Enter') gsend(); });
setInterval(poll, 3000); poll();
</script></body></html>"""


def make_web_app(store, ident: dict, on_web_text: Callable[[str], None],
                 save_dir_fn, channel_port: int,
                 send_text_fn=None) -> tuple[web.Application, Callable[[str], None]]:
    """Web app + push_text(ws_text) callback for pushing text into open sockets.

    The page is meant for OTHER devices: a visitor enters the PIN, then their
    text lands in our store via on_web_text and their files in save_dir_fn().
    send_text_fn (daemon's resolver) enables the page to send to remote
    devices and groups, turning the web view into a full client UI.
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
        target = ""
        while part := await reader.next():
            if part.name == "to":
                target = (await part.text()).strip()
                continue
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
            entry = {"name": part.filename, "size": size, "path": str(dest)}
            if target:
                from .files import send_file
                from .routing import resolve_target
                ip, port, fp = resolve_target(store, target)
                if ip:
                    r = await send_file(ip, str(dest),
                                        from_fp=ident["fingerprint"], to_fp=fp,
                                        port=port or DEVICE_PORT)
                    entry["forwarded"] = bool(r.get("ok"))
                else:
                    entry["forwarded"] = False
            saved.append(entry)
        return web.json_response({"ok": True, "saved": saved})

    async def devices_json(request: web.Request) -> web.Response:
        if request.query.get("pin") != pin():
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"devices": [
            {"fingerprint": d["fingerprint"],
             "alias": d.get("alias") or d["fingerprint"][:8]}
            for d in store.visible_devices(own=ident["fingerprint"])]})

    async def send_post(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad request"},
                                     status=400)
        if payload.get("pin") != pin():
            return web.json_response({"ok": False, "error": "unauthorized"},
                                     status=401)
        to = str(payload.get("to", ""))
        text = str(payload.get("text", "")).strip()
        if not text:
            return web.json_response({"ok": False, "error": "empty"})
        if to.startswith("group:"):
            from .groupchat import send_group_text
            return web.json_response(
                await send_group_text(store, ident, to[6:], text))
        if not send_text_fn:
            return web.json_response({"ok": False, "error": "no-route"})
        return web.json_response(await send_text_fn(to, text))

    # -- group transcript view (read-only; same PIN auth) --------------------

    async def group_list_page(request: web.Request) -> web.Response:
        if not authorized(request):
            raise web.HTTPFound("/")
        return web.Response(text=GROUP_LIST, content_type="text/html",
                            charset="utf-8")

    async def group_json(request: web.Request) -> web.Response:
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"groups": [
            {"id": g["id"], "name": g["name"], "members": len(g["members"])}
            for g in store.groups()]})

    async def group_page(request: web.Request) -> web.Response:
        if not authorized(request):
            raise web.HTTPFound("/")
        return web.Response(text=GROUP_PAGE, content_type="text/html",
                            charset="utf-8")

    async def group_data(request: web.Request) -> web.Response:
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from .groupchat import find_group
        g = find_group(store, request.match_info["gid"])
        if not g:
            return web.json_response({"error": "no-group"}, status=404)
        try:
            after = int(request.query.get("after", "0"))
        except ValueError:
            after = 0
        aliases = {d["fingerprint"]: d.get("alias") or d["fingerprint"][:8]
                   for d in store.devices()}
        me = ident["fingerprint"]
        items = []
        for m in store.group_history(g["id"], 50):
            if m["time"] <= after:
                continue
            c = m.get("content", "")
            if not isinstance(c, str):
                c = f"[文件 {c.get('name', '?')}]"
            items.append({"time": m["time"], "self": m["from"] == me,
                          "who": ("我" if m["from"] == me
                                  else aliases.get(m["from"], m["from"][:8])),
                          "text": c})
        items.sort(key=lambda x: x["time"])
        return web.json_response({"items": items})

    app = web.Application()
    app["fails"] = {}
    app.router.add_get("/", index)
    app.router.add_post("/validate-code", validate)
    # these must register before the /{pin} catch-all
    app.router.add_get("/group", group_list_page)
    app.router.add_get("/group/json", group_json)
    app.router.add_get("/group/{gid}", group_page)
    app.router.add_get("/group/{gid}/data", group_data)
    app.router.add_get("/devices", devices_json)
    app.router.add_post("/send", send_post)
    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/upload", upload)
    app.router.add_get("/{pin}", app_page)
    return app, push_text

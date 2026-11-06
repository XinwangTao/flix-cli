"""AP 配网页面：板子自己发 AP，手机/电脑连上来选要加入的 Wi-Fi。"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCAN_CACHE = "/run/wifi-scan.txt"
WPACONF = "/etc/wpa_supplicant.conf"
AP_START = "/opt/flixcli/scripts/wifi_ap_setup.sh"

PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RelayDesk 配网</title>
<style>
body{font-family:system-ui;background:#111;color:#eee;margin:0;padding:24px}
h1{font-size:20px;margin:0 0 4px}p.sub{opacity:.6;margin:0 0 20px;font-size:13px}
.net{display:flex;justify-content:space-between;padding:12px;border:1px solid #333;
border-radius:8px;margin-bottom:8px;cursor:pointer}
.net.on{border-color:#0a84ff;background:#12253a}
.sig{opacity:.7;font-size:12px}
input,button{width:100%;padding:12px;border-radius:8px;border:1px solid #333;
background:#1c1c1c;color:#eee;font-size:15px;box-sizing:border-box;margin-top:10px}
button{background:#0a84ff;border:none;font-weight:600}
.hide{display:none}#msg{margin-top:14px;font-size:14px;white-space:pre-wrap}
</style></head><body>
<h1>连接到 Wi-Fi</h1><p class="sub">选中一个网络，输入密码；连不上会自动退回配网模式。</p>
<div id="list"></div>
<div id="form" class="hide">
  <input id="ssid" placeholder="网络名称">
  <input id="psk" type="password" placeholder="密码（开放网络留空）">
  <button onclick="apply()">连接</button>
</div>
<button onclick="manual()">手动输入网络名</button>
<div id="msg"></div>
<script>
const nets = __NETS__;
const list = document.getElementById('list');
(nets||[]).forEach(n=>{
  const d=document.createElement('div'); d.className='net';
  d.innerHTML='<span>'+n.ssid+'</span><span class="sig">'+n.signal+'</span>';
  d.onclick=()=>{document.querySelectorAll('.net').forEach(x=>x.classList.remove('on'));
    d.classList.add('on');
    document.getElementById('ssid').value=n.ssid;
    document.getElementById('form').classList.remove('hide');
    document.getElementById('psk').focus();};
  list.appendChild(d);
});
function manual(){document.getElementById('form').classList.remove('hide');
  document.getElementById('ssid').focus();}
async function apply(){
  const ssid=document.getElementById('ssid').value.trim();
  const psk=document.getElementById('psk').value;
  const msg=document.getElementById('msg');
  if(!ssid){msg.textContent='请填网络名称';return;}
  msg.textContent='正在连接 '+ssid+' …（约 30 秒）';
  const r=await fetch('/apply',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({ssid:ssid,psk:psk})});
  const j=await r.json();
  msg.textContent=j.ok?('已连接，板子地址 '+j.ip+'；可以断开本热点去访问它。'):
    ('连接失败：'+(j.error||'未知原因')+'；已退回配网模式。');
}
</script></body></html>"""


def sh(cmd, timeout=40):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def parse_scan(path=SCAN_CACHE):
    try:
        raw = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    nets = []
    # 格式一：临时 supplicant 的 scan_results（bssid 	 freq 	 signal 	 flags 	 ssid）
    for line in raw.splitlines():
        parts = line.split("	")
        if len(parts) >= 5 and parts[4].strip() and parts[0] != "bssid / frequency / signal level / flags / ssid":
            nets.append({"ssid": parts[4].strip(), "signal": parts[2].strip()})
    if nets:
        return _dedupe(nets)
    # 格式二：iw dev scan 的原始输出
    ssid, signal = None, ""
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("SSID:"):
            ssid = line.split("SSID:", 1)[1].strip()
        elif line.startswith("signal:"):
            signal = line.split("signal:", 1)[1].strip()
        elif line.startswith("BSS "):
            if ssid and not any(n["ssid"] == ssid for n in nets):
                nets.append({"ssid": ssid, "signal": signal or "-"})
            ssid = None
    if ssid and not any(n["ssid"] == ssid for n in nets):
        nets.append({"ssid": ssid, "signal": signal or "-"})
    return _dedupe(nets)


def _dedupe(nets):
    nets = [n for n in nets if n["ssid"]][:30]

    def strength(n):
        try:
            return float(n["signal"].split()[0])
        except Exception:
            return -99

    nets.sort(key=strength, reverse=True)
    return nets


def connect(iface, ssid, psk):
    if not re.fullmatch(r"[\w\-. \u4e00-\u9fff]{1,32}", ssid):
        return {"ok": False, "error": "网络名称不合法"}
    if psk and not 8 <= len(psk) <= 63:
        return {"ok": False, "error": "密码需要 8-63 位"}
    body = '    psk="%s"\n' % psk if psk else "    key_mgmt=NONE\n"
    with open(WPACONF, "w", encoding="utf-8") as f:
        f.write("ctrl_interface=/run/wpa_supplicant\nupdate_config=1\ncountry=CN\n\n")
        f.write('network={\n    ssid="%s"\n%s    scan_ssid=1\n}\n' % (ssid, body))
    os.chmod(WPACONF, 0o600)

    sh("pkill -f hostapd-relaydesk")
    sh("pkill -f dnsmasq-relaydesk")
    sh("ip addr flush dev %s" % iface)
    sh("pkill -x wpa_supplicant")
    time.sleep(1)
    sh("wpa_supplicant -B -i %s -D nl80211 -c %s -O /run/wpa_supplicant" % (iface, WPACONF), 20)
    for _ in range(30):
        time.sleep(2)
        _, out = sh("wpa_cli -p /run/wpa_supplicant -i %s status" % iface)
        if "wpa_state=COMPLETED" in out:
            sh("udhcpc -i %s -b -R -t 6 -T 3" % iface, 40)
            time.sleep(2)
            _, addr = sh("ip -4 -o addr show %s" % iface)
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", addr)
            return {"ok": True, "ip": m.group(1) if m else "已连接但未取到地址"}
    sh("nohup bash %s >/var/log/wifi-ap.log 2>&1 &" % AP_START, 10)
    return {"ok": False, "error": "连不上（密码错误、信号太弱，或该热点要求 PMF）"}


class Handler(BaseHTTPRequestHandler):
    iface = "wlan0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        nets = parse_scan()
        if self.path.startswith("/status"):
            body = json.dumps({"iface": self.iface, "nets": nets}, ensure_ascii=False)
            return self._send(200, body.encode(), "application/json; charset=utf-8")
        page = PAGE.replace("__NETS__", json.dumps(nets, ensure_ascii=False))
        self._send(200, page.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        if not self.path.startswith("/apply"):
            return self._send(404, b"{}", "application/json")
        try:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, b'{"ok":false,"error":"bad request"}', "application/json")
        result = connect(self.iface, str(payload.get("ssid", "")).strip(),
                         str(payload.get("psk", "")))
        self._send(200, json.dumps(result, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="wlan0")
    # 板上 80 常被 nginx 占着，逐个试；绑定成功的端口写进 /run/wifi-provision.port
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--ports", default="8899,8900,8901")
    args = ap.parse_args()
    Handler.iface = args.iface
    candidates = [args.port] if args.port else [int(p) for p in args.ports.split(",") if p]
    server = None
    for port in candidates:
        try:
            server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print("没有可用端口（%s 都被占用）" % ",".join(map(str, candidates)), flush=True)
        return 2
    try:
        with open("/run/wifi-provision.port", "w", encoding="utf-8") as f:
            f.write(str(server.server_address[1]))
    except OSError:
        pass
    print("配网页面监听 %d" % server.server_address[1], flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

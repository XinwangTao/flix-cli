"""最小 HTTP/HTTPS(CONNECT) 代理，给没有 NAT 的板子临时上网用。

PC 上不用管理员权限：监听 0.0.0.0:3128，板子把 http_proxy/https_proxy 指过来即可。
"""
import json
import socket
import socketserver
import sys
import threading

BUF = 65536


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline(65536).decode("latin-1")
            if not line:
                return
            method, target, _ = (line.split() + ["", "", ""])[:3]
            headers = []
            while True:
                h = self.rfile.readline(65536)
                if h in (b"\r\n", b"\n", b""):
                    break
                headers.append(h.decode("latin-1").rstrip("\r\n"))
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                self._tunnel(host, int(port or 443))
                return
            self._forward_http(method, target, headers)
        except Exception:
            self._fail(502)

    def _fail(self, code):
        try:
            msg = {403: "Forbidden", 502: "Bad Gateway"}.get(code, "Error")
            self.wfile.write(("HTTP/1.1 %d %s\r\nContent-Length: 0\r\n\r\n" % (code, msg)).encode())
        except Exception:
            pass

    def _tunnel(self, host, port):
        try:
            upstream = socket.create_connection((host, port), timeout=20)
        except Exception:
            return self._fail(502)
        self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self.wfile.flush()
        self._pipe(self.connection, upstream)

    def _forward_http(self, method, target, headers):
        if not target.lower().startswith("http://"):
            return self._fail(403)
        rest = target[7:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        port = int(port or 80)
        try:
            upstream = socket.create_connection((host, port), timeout=20)
            req = "%s /%s HTTP/1.1\r\n" % (method, path)
            req += "\r\n".join(h for h in headers if not h.lower().startswith("proxy-"))
            upstream.sendall((req + "\r\n\r\n").encode("latin-1"))
        except Exception:
            return self._fail(502)
        self._pipe(self.connection, upstream)

    def _pipe(self, a, b):
        def pump(src, dst):
            try:
                while True:
                    data = src.recv(BUF)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass
        t = threading.Thread(target=pump, args=(b, a), daemon=True)
        t.start()
        pump(a, b)
        t.join(30)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3128
    print(json.dumps({"listen": port}), flush=True)
    Server(("0.0.0.0", port), Handler).serve_forever()

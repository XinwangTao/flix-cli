import re

p = r"D:\flix-cli\src\flixcli\server.py"
src = open(p, encoding="utf-8").read()

start = src.index("    @web.middleware")
end = src.index("    app = web.Application(middlewares=[_wire_log])") + len(
    "    app = web.Application(middlewares=[_wire_log])\n")

new = (
    "    @web.middleware\n"
    "    async def _wire_log(request, handler):\n"
    "        import time as _t\n"
    "        try:\n"
    "            body = await request.read()\n"
    "        except Exception:\n"
    "            body = b\"?\"\n"
    "        rec = (f\"[{_t.strftime('%H:%M:%S')}] {request.method} {request.path_qs}\\n\"\n"
    "               f\"headers={dict(request.headers)}\\n\"\n"
    "               f\"body={body[:2000]!r}\\n----\\n\")\n"
    "        with open(r\"D:/flix-cli/reverse/wire_capture.log\", \"ab\") as f:\n"
    "            f.write(rec.encode(\"utf-8\", \"replace\"))\n"
    "        return await handler(request)\n"
    "\n"
    "    app = web.Application(middlewares=[_wire_log])\n"
)
src = src[:start] + new + src[end:]
open(p, "w", encoding="utf-8").write(src)
print("repaired")

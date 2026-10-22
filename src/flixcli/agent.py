"""flix agent: poll incoming bubbles, hand them to an external command, reply.

L1 flow (docs/archify/flix_agent_workflow.html):
poll -> guard -> cmd -> send -> cursor

Guard rails exist because two agents on one LAN will otherwise answer each
other forever: paired peers only, skip our own prefix, per-peer gap, and a hop
counter carried in a trailing [[hop=N]] marker.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .channel import PeerError, post_bubble
from .config import APP_DIR, Store, load_or_create_identity
from .groupchat import send_group_text
from .model import text_bubble
from .routing import resolve_target

STATE_PATH = APP_DIR / "agent_state.json"
LOG_PATH = APP_DIR / "agent.log"
HOP_PREFIX = "[[hop="


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_state() -> dict | None:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_state(state: dict) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError as e:
        log(f"[agent] cannot save state: {e}")


def parse_hop(text: str) -> int:
    """Hop counter carried as a trailing [[hop=N]] marker (0 when absent)."""
    i = text.rfind(HOP_PREFIX)
    if i < 0:
        return 0
    try:
        return int(text[i + len(HOP_PREFIX):].split("]", 1)[0])
    except ValueError:
        return 0


def strip_hop(text: str) -> str:
    i = text.rfind(HOP_PREFIX)
    if i < 0:
        return text
    return text[:i].rstrip()


def run_command(cmd: list[str], payload: dict, env_extra: dict,
                timeout: float) -> tuple[int, str, str]:
    env = dict(os.environ)
    env.update(env_extra)
    try:
        proc = subprocess.run(cmd, input=json.dumps(payload, ensure_ascii=False),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=env)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"
    except OSError as e:
        return -1, "", f"cannot run {cmd[0]!r}: {e}"


class Agent:
    def __init__(self, store: Store, ident: dict, *, cmd: list[str],
                 interval: float = 2.0, prefix: str = "[A-AI]",
                 min_gap: float = 30.0, max_hop: int = 2,
                 timeout: float = 60.0, dry_run: bool = False):
        self.store = store
        self.ident = ident
        self.cmd = cmd
        self.interval = interval
        self.prefix = prefix
        self.min_gap = min_gap
        self.max_hop = max_hop
        self.timeout = timeout
        self.dry_run = dry_run
        self.state = load_state()
        if self.state is None or "cursor" not in self.state:
            # Fresh start: never answer the backlog that piled up while we were off.
            existing = store.bubbles(limit=1)
            self.state = {"cursor": existing[0]["time"] if existing
                          else int(time.time() * 1000), "last_reply": {}}
            save_state(self.state)

    # -- guard ---------------------------------------------------------------
    def accept(self, bubble: dict) -> tuple[bool, str]:
        me = self.ident["fingerprint"]
        if bubble["from"] == me:
            return False, "self"
        if not self.store.is_paired(bubble["from"]):
            return False, "unpaired"
        text = bubble.get("content")
        if not isinstance(text, str):
            return False, "not-text"
        if text.lstrip().startswith(self.prefix):
            return False, "own-prefix"
        if parse_hop(text) >= self.max_hop:
            return False, "hop-limit"
        last = self.state.get("last_reply", {}).get(bubble["from"], 0)
        if time.time() - last < self.min_gap:
            return False, "rate-limit"
        return True, "ok"

    # -- AI call -------------------------------------------------------------
    def invoke(self, bubble: dict, hop: int) -> tuple[bool, str]:
        alias = self.ident.get("alias", "")
        payload = {
            "id": bubble["id"],
            "from": bubble["from"],
            "groupId": bubble.get("groupId", ""),
            "text": strip_hop(bubble["content"]),
            "time": bubble["time"],
            "hop": hop,
        }
        env = {"FLIX_SELF_FP": self.ident["fingerprint"],
               "FLIX_SELF_ALIAS": alias}
        rc, out, err = run_command(self.cmd, payload, env, self.timeout)
        if rc != 0:
            log(f"[agent] cmd failed rc={rc}: {err.strip()[:200]}")
            return False, ""
        reply = out.strip()
        if not reply:
            log("[agent] cmd produced no output; not replying")
            return False, ""
        return True, reply

    # -- send ----------------------------------------------------------------
    async def reply(self, bubble: dict, text: str) -> bool:
        gid = bubble.get("groupId") or ""
        if self.dry_run:
            log(f"[agent] dry-run reply -> {gid or bubble['from']}: {text[:120]}")
            return True
        if gid:
            result = await send_group_text(self.store, self.ident, gid, text)
            if result.get("ok"):
                log(f"[agent] replied group {gid[:8]} "
                    f"({result.get('delivered')}/{result.get('total')}): {text[:120]}")
            else:
                log(f"[agent] group reply failed: {result.get('results')}")
            return bool(result.get("ok"))
        ip, port, fp = resolve_target(self.store, bubble["from"])
        if not ip:
            log(f"[agent] no route to {bubble['from'][:8]}; device not in store")
            return False
        try:
            reply = await post_bubble(ip, text_bubble(self.ident["fingerprint"], fp, text),
                                      port=port or 18991)
            log(f"[agent] replied {fp[:8]} ({reply}): {text[:120]}")
            return True
        except PeerError as e:
            log(f"[agent] send failed: {e.kind} {e.detail}")
            return False

    # -- loop ----------------------------------------------------------------
    async def tick(self) -> int:
        cursor = self.state.get("cursor", 0)
        seen = [b for b in self.store.bubbles(limit=200) if b["time"] > cursor]
        seen.sort(key=lambda b: b["time"])
        handled = 0
        for bubble in seen:
            ok, why = self.accept(bubble)
            if not ok:
                if why != "self":
                    log(f"[agent] skip {bubble['id'][:8]} ({why})")
                self.state["cursor"] = max(cursor, bubble["time"])
                cursor = self.state["cursor"]
                continue
            hop = parse_hop(bubble["content"]) + 1
            good, text = self.invoke(bubble, hop)
            if good:
                text = f"{self.prefix} {text} {HOP_PREFIX}{hop}]]"
                if await self.reply(bubble, text):
                    self.state.setdefault("last_reply", {})[bubble["from"]] = time.time()
                    handled += 1
            self.state["cursor"] = max(cursor, bubble["time"])
            cursor = self.state["cursor"]
            save_state(self.state)
        if seen:
            save_state(self.state)
        return handled

    async def run(self, once: bool = False) -> int:
        log(f"[agent] started: cmd={' '.join(self.cmd)!r} interval={self.interval}s "
            f"prefix={self.prefix!r} gap={self.min_gap}s hop<={self.max_hop}")
        while True:
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001 - keep the loop alive
                log(f"[agent] tick error: {e}")
            if once:
                return 0
            await asyncio.sleep(self.interval)


def build_cmd(arg: str) -> list[str]:
    return shlex.split(arg, posix=(os.name != "nt"))


def main(args) -> int:
    store = Store()
    try:
        ident = load_or_create_identity(store)
        cmd = build_cmd(args.cmd)
        if not cmd:
            print("flix agent: empty --cmd", file=sys.stderr)
            return 2
        agent = Agent(store, ident, cmd=cmd, interval=args.interval,
                      prefix=args.prefix, min_gap=args.min_gap,
                      max_hop=args.max_hop, timeout=args.timeout,
                      dry_run=args.dry_run)
        return asyncio.run(agent.run(once=args.once))
    except KeyboardInterrupt:
        return 130
    finally:
        store.close()

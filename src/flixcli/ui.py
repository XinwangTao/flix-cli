"""flix ui: native desktop window for LAN group conversations.

Reads the local store directly (no web), polls every 2 s, and posts through
send_group_text. Ships as the `flix ui` subcommand and flixui.exe.
"""
from __future__ import annotations

import asyncio
import threading
import time
import tkinter as tk
from tkinter import scrolledtext, ttk

from .config import Store, load_or_create_identity
from .groupchat import find_group, send_group_text

POLL_MS = 2000


def _fmt_row(m: dict, me: str, aliases: dict) -> tuple[int, str, str]:
    c = m.get("content", "")
    if not isinstance(c, str):
        c = f"[文件 {c.get('name', '?')}]"
    if str(m.get("from", "")) == me:
        who = "我"
    else:
        who = aliases.get(str(m.get("from", "")), str(m.get("from", ""))[:8])
    return m["time"], who, c


class UiApp:
    def __init__(self, store: Store, ident: dict) -> None:
        self.store = store
        self.me = ident["fingerprint"]
        self.groups = store.groups()
        self.gid = self.groups[0]["id"] if self.groups else ""
        self.cursor = 0
        self.root = tk.Tk()
        self.root.title("Flix 群聊 — DESKTOP-K087HI9-R2")
        self.root.geometry("760x560")
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(top, text="群：").pack(side="left")
        self.box = ttk.Combobox(top, values=[g["name"] for g in self.groups],
                                state="readonly", width=24)
        if self.groups:
            self.box.current(0)
            self.box.bind("<<ComboboxSelected>>", lambda e: self.switch())
        self.box.pack(side="left", padx=4)
        self.status = ttk.Label(top, text="身份 " + self.me[:8])
        self.status.pack(side="right")
        self.text = scrolledtext.ScrolledText(self.root, state="disabled",
                                              wrap="word", font=("Microsoft YaHei", 10))
        self.text.pack(fill="both", expand=True, padx=8, pady=4)
        for tag, fg in (("me", "#4da3ff"), ("meta", "#888"), ("other", "#eeeeee")):
            self.text.tag_configure(tag, foreground=fg)
            self.text.tag_configure(tag + "meta", foreground="#666",
                                    font=("Microsoft YaHei", 8))
        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=8, pady=(2, 8))
        self.entry = ttk.Entry(bottom)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self.post())
        ttk.Button(bottom, text="发送到群", command=self.post).pack(side="left", padx=4)
        if self.gid:
            self.switch()
        self.root.after(POLL_MS, self._poll)

    def switch(self) -> None:
        name = self.box.get()
        g = find_group(self.store, name)
        self.gid = g["id"] if g else ""
        self.cursor = 0
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def _append(self, ts: int, who: str, text: str) -> None:
        mine = who == "我"
        self.text.configure(state="normal")
        tag = "me" if mine else "other"
        self.text.insert("end", f"{who}  ", tag)
        self.text.insert("end",
                         time.strftime("%H:%M:%S", time.localtime(ts / 1000)) + "\n",
                         "metameta")
        self.text.insert("end", text + "\n\n", tag)
        self.text.configure(state="disabled")
        self.text.see("end")

    def _poll(self) -> None:
        try:
            if self.gid:
                aliases = {d["fingerprint"]: d.get("alias") or d["fingerprint"][:8]
                           for d in self.store.devices()}
                rows = [r for r in self.store.group_history(self.gid, 200)
                        if r["time"] > self.cursor]
                for m in sorted(rows, key=lambda x: x["time"]):
                    ts, who, c = _fmt_row(m, self.me, aliases)
                    self._append(ts, who, c)
                    self.cursor = max(self.cursor, m["time"])
        except Exception as e:  # noqa: BLE001 - keep the UI alive
            self.status.configure(text=f"err {e}")
        self.root.after(POLL_MS, self._poll)

    def post(self) -> None:
        text = self.entry.get().strip()
        if not text or not self.gid:
            return
        self.entry.delete(0, "end")

        def run() -> None:
            asyncio.run(send_group_text(self.store,
                                        {"fingerprint": self.me}, self.gid, text))
        threading.Thread(target=run, daemon=True).start()


def main(args=None) -> int:
    store = Store()
    ident = load_or_create_identity(store)
    app = UiApp(store, ident)
    if args and getattr(args, "group", None):
        g = find_group(store, args.group)
        if g:
            app.box.set(g["name"])
            app.switch()
    app.root.mainloop()
    store.close()
    return 0

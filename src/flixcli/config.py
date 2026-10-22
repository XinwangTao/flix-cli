"""Local state: identity, preferences and sqlite store.

Layout mirrors the official client (db.sqlite tables observed via drift schema),
kept under %APPDATA%/flixcli to avoid touching the official installation.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from . import DEVICE_PORT

APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "flixcli"
DB_PATH = APP_DIR / "db.sqlite"
PREFS_PATH = APP_DIR / "shared_preferences.json"


def ensure_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS persistence_devices (
  fingerprint TEXT NOT NULL PRIMARY KEY,
  alias TEXT, device_model TEXT, device_type INTEGER,
  port INTEGER, version INTEGER, ip TEXT, host TEXT, "from" TEXT,
  insert_or_update_time INTEGER NOT NULL DEFAULT 0, os TEXT
);
CREATE TABLE IF NOT EXISTS pair_devices (
  fingerprint TEXT NOT NULL PRIMARY KEY,
  code TEXT NOT NULL,
  insert_or_update_time INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS blocked_devices (
  device_id TEXT NOT NULL PRIMARY KEY,
  device_name TEXT,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bubble_entities (
  id TEXT NOT NULL PRIMARY KEY,
  from_device TEXT NOT NULL,
  to_device TEXT NOT NULL,
  type INTEGER NOT NULL,
  time INTEGER NOT NULL DEFAULT 9223372036854775807,
  group_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS text_contents (
  id TEXT NOT NULL PRIMARY KEY,
  content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS file_contents (
  id TEXT NOT NULL PRIMARY KEY,
  group_id TEXT NOT NULL DEFAULT '',
  resource_id TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  name_with_suffix TEXT NOT NULL,
  size INTEGER NOT NULL,
  path TEXT,
  state INTEGER NOT NULL,
  progress REAL NOT NULL,
  speed INTEGER NOT NULL DEFAULT 0,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  waiting_for_accept INTEGER NOT NULL DEFAULT 1,
  extra TEXT,
  origin_path TEXT,
  hash TEXT
);
CREATE TABLE IF NOT EXISTS save_locations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filetype TEXT NOT NULL,
  location TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS groups (
  id TEXT NOT NULL PRIMARY KEY,
  name TEXT NOT NULL,
  created_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS group_members (
  group_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  PRIMARY KEY (group_id, fingerprint)
);
"""


class Store:
    def __init__(self, path: Path | None = None):
        ensure_dirs()
        self.path = Path(path) if path else DB_PATH
        self.con = sqlite3.connect(self.path, timeout=2.0)
        with contextlib.suppress(Exception):
            self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript(SCHEMA)
        with contextlib.suppress(Exception):
            self.con.execute("ALTER TABLE groups ADD COLUMN code TEXT")
        self.con.commit()

    # -- devices -------------------------------------------------------------
    def upsert_device(self, fingerprint: str, *, alias: str = "", device_model: str = "",
                      port: int | None = None, version: int | None = None,
                      ip: str = "", os_name: str = "") -> None:
        self.con.execute(
            "INSERT INTO persistence_devices (fingerprint, alias, device_model, port,"
            " version, ip, os, insert_or_update_time, host, \"from\")"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(fingerprint) DO UPDATE SET alias=excluded.alias,"
            " device_model=excluded.device_model, port=excluded.port,"
            " version=excluded.version, ip=excluded.ip, os=excluded.os,"
            " insert_or_update_time=excluded.insert_or_update_time",
            (fingerprint, alias, device_model, port, version, ip, os_name,
             int(time.time() * 1000), "", ""))
        self.con.commit()

    def devices(self) -> list[dict]:
        cur = self.con.execute(
            "SELECT fingerprint, alias, device_model, port, version, ip, os,"
            " insert_or_update_time FROM persistence_devices ORDER BY alias")
        cols = ["fingerprint", "alias", "deviceModel", "port", "version", "ip", "os", "seenAt"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def is_paired(self, fingerprint: str) -> bool:
        return self.con.execute(
            "SELECT 1 FROM pair_devices WHERE fingerprint=?", (fingerprint,)
        ).fetchone() is not None

    def add_pair(self, fingerprint: str, code: str) -> None:
        self.con.execute(
            "INSERT INTO pair_devices (fingerprint, code, insert_or_update_time)"
            " VALUES (?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET code=excluded.code,"
            " insert_or_update_time=excluded.insert_or_update_time",
            (fingerprint, code, int(time.time() * 1000)))
        self.con.commit()

    def own_web_pin(self) -> str:
        """4-digit web send-page PIN (rotates if absent), prefs web_pin."""
        p = self.prefs()
        v = p.get("web_pin")
        if not v:
            import secrets
            v = f"{secrets.randbelow(10000):04d}"
            self.set_pref("web_pin", v)
        return v

    def group_by_code(self, code: str) -> dict | None:
        row = self.con.execute(
            "SELECT id, name FROM groups WHERE code=?", (str(code),)).fetchone()
        if not row:
            return None
        members = [r[0] for r in self.con.execute(
            "SELECT fingerprint FROM group_members WHERE group_id=?", (row[0],))]
        return {"id": row[0], "name": row[1], "members": members}

    def set_group_code(self, group_id: str, code: str) -> None:
        self.con.execute("UPDATE groups SET code=? WHERE id=?", (str(code), group_id))
        self.con.commit()

    def paired_targets(self) -> list[tuple[str, str, int]]:
        """[(fingerprint, ip, port)] for paired devices that have an address."""
        rows = self.con.execute(
            "SELECT p.fingerprint, COALESCE(d.ip, ''), COALESCE(d.port, ?)"
            " FROM pair_devices p LEFT JOIN persistence_devices d"
            " ON d.fingerprint = p.fingerprint", (DEVICE_PORT,)).fetchall()
        return [(fp, ip, int(port or DEVICE_PORT)) for fp, ip, port in rows
                if ip and ip.replace('.', '').isdigit()]

    def pair_code(self, fingerprint: str) -> str | None:
        row = self.con.execute(
            "SELECT code FROM pair_devices WHERE fingerprint=?", (fingerprint,)).fetchone()
        return row[0] if row else None

    def own_pair_key(self) -> str:
        """Our 4-digit pairing key (official pref key: device_pair_key)."""
        p = self.prefs()
        key = p.get("device_pair_key")
        if not key:
            import secrets
            key = f"{secrets.randbelow(10000):04d}"
            self.set_pref("device_pair_key", key)
        return key

    # -- bubbles -------------------------------------------------------------
    def add_text_bubble(self, bubble_id: str, from_dev: str, to_dev: str,
                        content: str, ts: int, group_id: str = "") -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO bubble_entities (id, from_device, to_device, type,"
            " time, group_id) VALUES (?,?,?,0,?,?)",
            (bubble_id, from_dev, to_dev, ts, group_id))
        self.con.execute(
            "INSERT OR REPLACE INTO text_contents (id, content) VALUES (?,?)",
            (bubble_id, content))
        self.con.commit()

    def bubbles(self, limit: int = 50) -> list[dict]:
        cur = self.con.execute(
            "SELECT b.id, b.from_device, b.to_device, b.type, b.time, b.group_id,"
            " t.content, f.name, f.size, f.state, f.progress"
            " FROM bubble_entities b LEFT JOIN text_contents t ON t.id=b.id"
            " LEFT JOIN file_contents f ON f.id=b.id"
            " ORDER BY b.time DESC LIMIT ?", (limit,))
        out = []
        for (bid, frm, to, typ, ts, gid, text, fname, fsize, fstate, fprog) in cur.fetchall():
            item = {"id": bid, "from": frm, "to": to, "type": typ, "time": ts, "groupId": gid}
            if text is not None:
                item["content"] = text
            elif fname is not None:
                item["content"] = {"name": fname, "size": fsize, "state": fstate,
                                   "progress": fprog}
            out.append(item)
        return out

    # -- groups --------------------------------------------------------------
    def create_group(self, gid: str, name: str) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO groups (id, name, created_at) VALUES (?,?,?)",
            (gid, name, int(time.time() * 1000)))
        self.con.commit()

    def delete_group(self, gid: str) -> None:
        self.con.execute("DELETE FROM groups WHERE id=?", (gid,))
        self.con.execute("DELETE FROM group_members WHERE group_id=?", (gid,))
        self.con.commit()

    def add_group_member(self, gid: str, fingerprint: str) -> None:
        self.con.execute(
            "INSERT OR IGNORE INTO group_members (group_id, fingerprint) VALUES (?,?)",
            (gid, fingerprint))
        self.con.commit()

    def remove_group_member(self, gid: str, fingerprint: str) -> None:
        self.con.execute(
            "DELETE FROM group_members WHERE group_id=? AND fingerprint=?",
            (gid, fingerprint))
        self.con.commit()

    def groups(self) -> list[dict]:
        cur = self.con.execute("SELECT id, name, created_at FROM groups ORDER BY created_at")
        out = []
        for gid, name, ts in cur.fetchall():
            members = [r[0] for r in self.con.execute(
                "SELECT fingerprint FROM group_members WHERE group_id=? ORDER BY fingerprint",
                (gid,))]
            out.append({"id": gid, "name": name, "createdAt": ts, "members": members})
        return out

    def group_members(self, gid: str) -> list[str]:
        return [r[0] for r in self.con.execute(
            "SELECT fingerprint FROM group_members WHERE group_id=?", (gid,))]

    def group_history(self, gid: str, limit: int = 50) -> list[dict]:
        cur = self.con.execute(
            "SELECT b.id, b.from_device, b.to_device, b.type, b.time, t.content,"
            " f.name, f.size"
            " FROM bubble_entities b LEFT JOIN text_contents t ON t.id=b.id"
            " LEFT JOIN file_contents f ON f.id=b.id"
            " WHERE b.group_id=? ORDER BY b.time DESC LIMIT ?", (gid, limit))
        out = []
        for (bid, frm, to, typ, ts, text, fname, fsize) in cur.fetchall():
            item = {"id": bid, "from": frm, "to": to, "type": typ, "time": ts}
            if text is not None:
                item["content"] = text
            elif fname is not None:
                item["content"] = {"name": fname, "size": fsize}
            out.append(item)
        return out

    # -- prefs ---------------------------------------------------------------
    def prefs(self) -> dict:
        if PREFS_PATH.exists():
            return json.loads(PREFS_PATH.read_text(encoding="utf-8"))
        return {}

    def set_pref(self, key: str, value) -> None:
        p = self.prefs()
        p[key] = value
        tmp = PREFS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(p, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, PREFS_PATH)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def load_or_create_identity(store: Store) -> dict:
    """fingerprint (uuid4) + alias; mirrors deviceModal fields."""
    p = store.prefs()
    ident = p.get("identity")
    if not ident:
        ident = {
            "fingerprint": str(uuid.uuid4()),
            "alias": os.environ.get("COMPUTERNAME", "flixd"),
            "deviceModel": "Windows",
            "deviceType": "desktop",
            "os": "windows",
        }
        store.set_pref("identity", ident)
    return ident

"""Local state: identity, preferences and sqlite store.

Layout mirrors the official client (db.sqlite tables observed via drift schema),
kept under %APPDATA%/flixcli to avoid touching the official installation.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from . import DEVICE_PORT

APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "flixcli"
# APPDATA occasionally points at the state dir itself (tests, shell exports);
# appending "flixcli" again would mint a second identity under flixcli/flixcli.
if APP_DIR.parent.name.lower() == "flixcli":
    APP_DIR = APP_DIR.parent
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
  created_at INTEGER NOT NULL DEFAULT 0,
  rev INTEGER NOT NULL DEFAULT 0
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
        # Prefs sit next to the database. A test that opens a temp database must
        # not rewrite the real install's prefs — that is how a temp saveDir and
        # a 32-byte receive cap once leaked into production.
        self.prefs_path = (PREFS_PATH if path is None
                           else self.path.with_name("shared_preferences.json"))
        self.con = sqlite3.connect(self.path, timeout=2.0)
        with contextlib.suppress(Exception):
            self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript(SCHEMA)
        with contextlib.suppress(Exception):
            self.con.execute("ALTER TABLE groups ADD COLUMN code TEXT")
        with contextlib.suppress(Exception):
            self.con.execute(
                "ALTER TABLE groups ADD COLUMN rev INTEGER NOT NULL DEFAULT 0")
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
            " t.content, f.name, f.size, f.state, f.progress, f.path, f.origin_path"
            " FROM bubble_entities b LEFT JOIN text_contents t ON t.id=b.id"
            " LEFT JOIN file_contents f ON f.id=b.id"
            " ORDER BY b.time DESC LIMIT ?", (limit,))
        out = []
        for (bid, frm, to, typ, ts, gid, text, fname, fsize, fstate, fprog,
             fpath, forigin) in cur.fetchall():
            item = {"id": bid, "from": frm, "to": to, "type": typ, "time": ts, "groupId": gid}
            if text is not None:
                item["content"] = text
            elif fname is not None:
                content = {"name": fname, "size": fsize, "state": fstate,
                           "progress": fprog}
                # The UI needs the landed file to render an image bubble or to
                # open the attachment; without it a received picture is just a
                # name string.
                if fpath:
                    content["path"] = fpath
                if forigin:
                    content["originPath"] = forigin
                item["content"] = content
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

    def group_rev(self, gid: str) -> int:
        """-1 = unknown group, so any incoming snapshot wins."""
        row = self.con.execute("SELECT rev FROM groups WHERE id=?", (gid,)).fetchone()
        return int(row[0]) if row and row[0] is not None else -1

    def merge_group_sync(self, gid: str, name: str, members: list[str],
                         rev: int) -> bool:
        """Full-snapshot merge, last-writer-wins by rev (v2C).

        Only a strictly newer revision overwrites the name and the member list;
        equal rev keeps local state so two nodes cannot ping-pong forever.
        """
        known = self.group_rev(gid)
        if known >= 0 and rev <= known:
            return False
        with self.con:
            self.con.execute(
                "INSERT INTO groups (id, name, created_at, rev) VALUES (?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name, rev=excluded.rev",
                (gid, name, int(time.time() * 1000), int(rev)))
            self.con.execute("DELETE FROM group_members WHERE group_id=?", (gid,))
            self.con.executemany(
                "INSERT OR IGNORE INTO group_members (group_id, fingerprint)"
                " VALUES (?,?)", [(gid, m) for m in members])
        return True

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
        if not self.prefs_path.exists():
            return {}
        try:
            return json.loads(self.prefs_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Keep the damaged file for inspection instead of overwriting it.
            bad = self.prefs_path.with_name(
                f"{self.prefs_path.name}.bad-{int(time.time())}")
            with contextlib.suppress(OSError):
                self.prefs_path.replace(bad)
            print(f"[flixcli] prefs unreadable, moved to {bad}", file=sys.stderr)
            return {}

    def set_pref(self, key: str, value) -> None:
        p = self.prefs()
        p[key] = value
        tmp = self.prefs_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(p, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.prefs_path)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _valid_identity(value) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        uuid.UUID(str(value.get("fingerprint")))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def _legacy_prefs_files() -> list[Path]:
    """State dirs older builds may have used; only near the canonical one.

    When APPDATA is set we look at siblings/nested dirs only: a sandbox or a
    second profile must not inherit the identity of an unrelated home dir.
    """
    base = os.environ.get("APPDATA", "").strip()
    if base:
        candidates = [APP_DIR / "flixcli", Path(base) / "flixcli"]
    else:
        home = Path.home()
        candidates = [home / "flixcli", home / "AppData" / "Roaming" / "flixcli"]
    out, seen = [], set()
    for path in candidates:
        key = str(path).lower()
        if path == APP_DIR or key in seen:
            continue
        seen.add(key)
        out.append(path / "shared_preferences.json")
    return out


def _adopt_legacy_identity() -> dict | None:
    """Reuse the newest identity from a stale state dir instead of minting one."""
    found: list[tuple[float, Path, dict]] = []
    for path in _legacy_prefs_files():
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ident = data.get("identity")
        if _valid_identity(ident):
            found.append((path.stat().st_mtime, path.parent, ident))
    if not found:
        return None
    found.sort(key=lambda item: item[0], reverse=True)
    if len(found) > 1:
        names = ", ".join(str(d) for _, d, _ in found)
        print(f"[flixcli] multiple legacy identities ({names}); "
              f"adopting {found[0][1]}", file=sys.stderr)
    mtime, source, ident = found[0]
    adopted = dict(ident)
    adopted["adopted_from"] = str(source)
    adopted.setdefault("created_at", int(mtime * 1000))
    return adopted


def load_or_create_identity(store: Store) -> dict:
    """fingerprint (uuid4) + alias; mirrors deviceModal fields.

    Prefs loss used to mint a fresh uuid, so peers saw the same PC twice.
    Now the newest legacy identity is adopted and written back to the
    canonical state dir; a new uuid is only created when none exists.
    """
    p = store.prefs()
    ident = p.get("identity")
    if _valid_identity(ident):
        return ident
    adopted = _adopt_legacy_identity()
    if adopted:
        store.set_pref("identity", adopted)
        return adopted
    ident = {
        "fingerprint": str(uuid.uuid4()),
        "alias": os.environ.get("COMPUTERNAME", "flixd"),
        "deviceModel": "Windows",
        "deviceType": "desktop",
        "os": "windows",
        "created_at": int(time.time() * 1000),
    }
    store.set_pref("identity", ident)
    return ident

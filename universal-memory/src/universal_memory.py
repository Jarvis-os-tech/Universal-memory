#!/usr/bin/env python3
"""Universal Memory - one shared memory for every agent on this machine.

Command: universal-memory
MCP server: `universal` -> tools universal_memory_put / _get / _append /
            _search / _list / _delete

One SQLite+FTS5 store, many agents (Hermes, OpenCode, Claude Code, Codex,
personal agents, browser agents through MCP).

Contract:
  read / write / append / search / list  -> free, no prompt
  delete                                 -> ALWAYS needs a human
                                             * interactive TTY: y/N prompt
                                             * non-interactive: returns a
                                               short-lived approval token that
                                               only `universal-memory approve`
                                               (a human at a shell) can redeem
  approve / deny                          -> CLI only, never exposed as an MCP
                                             tool, so an agent cannot self-approve

Interfaces:
  universal-memory ...       local agents (CLI, JSON output with --json)
  universal-memory mcp       MCP server over stdio
  universal-memory serve     MCP Streamable HTTP server (for browser agents)

`umem` remains available as a legacy alias for the command.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"
SERVER_NAME = "universal"
PROG = "universal-memory"

# Human descriptions for well-known namespaces, shown in the dashboard so a
# namespace is never just an opaque word. Unknown namespaces render blank.
NS_INFO = {
    "global": "Default bucket when no namespace is given.",
    "shared": "No single owner — facts every agent on this machine may read.",
    "hermes": "Hermes Agent's own notes (sessions, cron, kanban workers).",
    "opencode": "OpenCode sessions and their subagents.",
    "claude-code": "Claude Code sessions.",
    "codex": "OpenAI Codex sessions.",
    "cursor": "Cursor agent sessions.",
    "gemini-cli": "Gemini CLI sessions.",
    "aider": "Aider sessions.",
    "user": "What the agents believe about you — preferences, hardware, style.",
    "browser": "Browser-based assistants (ChatGPT, Grok, Perplexity, ...).",
    "chatgpt": "Stored by ChatGPT through the HTTP MCP endpoint.",
    "grok": "Stored by Grok through the HTTP MCP endpoint.",
    "perplexity": "Stored by Perplexity through the HTTP MCP endpoint.",
}

# --------------------------------------------------------------------------
# paths & config
# --------------------------------------------------------------------------

XDG_DATA = pathlib.Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
XDG_CONFIG = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()

DEFAULTS = {
    "db": str(XDG_DATA / "universal-memory" / "memory.db"),
    "default_ns": "global",
    "max_value_bytes": 1_000_000,
    "approval_ttl_seconds": 900,
    "audit": True,
    "http": {
        # `serve` — the MCP endpoint browser agents talk to, meant to stay up
        "host": "127.0.0.1",
        "port": 8787,
        # `web` — the dashboard + JSON API, started only when wanted
        "web_port": 8788,
        "token": "",
    },
}


def _env(*names: str) -> str | None:
    """New name first, legacy UMEM_* still accepted."""
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return None


CONFIG_PATH = pathlib.Path(
    _env("UNIVERSAL_MEMORY_CONFIG", "UMEM_CONFIG")
    or str(XDG_CONFIG / "universal-memory" / "config.json")
)


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg = _deep_merge(cfg, json.loads(CONFIG_PATH.read_text()))
        except (json.JSONDecodeError, OSError) as e:
            raise SystemExit(f"{PROG}: unreadable config {CONFIG_PATH}: {e}")
    if _env("UNIVERSAL_MEMORY_DB", "UMEM_DB"):
        cfg["db"] = _env("UNIVERSAL_MEMORY_DB", "UMEM_DB")
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    tmp.chmod(0o600)
    tmp.replace(CONFIG_PATH)


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

class UmemError(Exception):
    exit_code = 1


class NotFound(UmemError):
    exit_code = 2


class ApprovalRequired(UmemError):
    exit_code = 3


class Denied(UmemError):
    exit_code = 4


# --------------------------------------------------------------------------
# actor identity
# --------------------------------------------------------------------------

def detect_actor() -> str:
    """Who is calling? Stable id recorded on every write and audit row."""
    if _env("UNIVERSAL_MEMORY_ACTOR", "UMEM_ACTOR"):
        return _env("UNIVERSAL_MEMORY_ACTOR", "UMEM_ACTOR")

    markers = [
        ("HERMES_SESSION_ID", "hermes"),
        ("HERMES_AGENT", "hermes"),
        ("OPENCODE", "opencode"),
        ("CLAUDECODE", "claude-code"),
        ("CLAUDE_CODE_ENTRYPOINT", "claude-code"),
        ("CODEX_SANDBOX", "codex"),
        ("CODEX_HOME", "codex"),
        ("CURSOR_AGENT", "cursor"),
        ("AIDER", "aider"),
        ("GEMINI_CLI", "gemini-cli"),
        ("ANTHROPIC_TASK", "anthropic-task"),
    ]
    for env, name in markers:
        if os.environ.get(env):
            return f"{name}:{os.environ.get(env, '')}".rstrip(":")

    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USER", "unknown")
    return f"{user}@{socket.gethostname()}"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ns         TEXT    NOT NULL,
  key        TEXT    NOT NULL,
  type       TEXT    NOT NULL DEFAULT 'kv' CHECK (type IN ('kv','log')),
  value      TEXT    NOT NULL DEFAULT '',
  tags       TEXT    NOT NULL DEFAULT '',
  actor      TEXT    NOT NULL DEFAULT '',
  created_at TEXT    NOT NULL,
  updated_at TEXT    NOT NULL,
  version    INTEGER NOT NULL DEFAULT 1,
  UNIQUE (ns, key)
);

CREATE INDEX IF NOT EXISTS idx_memory_ns ON memory (ns);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  key, value, tags, content='memory', content_rowid='id', tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
  INSERT INTO memory_fts(rowid, key, value, tags)
  VALUES (new.id, new.key, new.value, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
  INSERT INTO memory_fts(memory_fts, rowid, key, value, tags)
  VALUES ('delete', old.id, old.key, old.value, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
  INSERT INTO memory_fts(memory_fts, rowid, key, value, tags)
  VALUES ('delete', old.id, old.key, old.value, old.tags);
  INSERT INTO memory_fts(rowid, key, value, tags)
  VALUES (new.id, new.key, new.value, new.tags);
END;

CREATE TABLE IF NOT EXISTS audit (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      TEXT NOT NULL,
  op      TEXT NOT NULL,
  ns      TEXT NOT NULL,
  key     TEXT NOT NULL,
  actor   TEXT NOT NULL,
  detail  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS approval (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  token        TEXT    NOT NULL UNIQUE,
  op           TEXT    NOT NULL,
  ns           TEXT    NOT NULL,
  key          TEXT    NOT NULL,
  snapshot     TEXT    NOT NULL DEFAULT '',
  actor        TEXT    NOT NULL,
  requested_at TEXT    NOT NULL,
  expires_at   TEXT    NOT NULL,
  state        TEXT    NOT NULL DEFAULT 'pending'
);
"""


class Store:
    def __init__(self, path: str):
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # shared between agents on this machine, but not with other OS users
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        # SQLite connections are not cross-thread safe, and the HTTP MCP server
        # hands every request to a new thread: keep one connection per thread.
        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT OR IGNORE INTO meta(k,v) VALUES ('schema_version','1')")

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10, isolation_level=None,
                                check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=10000")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
            with self._lock:
                self._all_conns.append(c)
        return c

    def close(self) -> None:
        with self._lock:
            conns, self._all_conns = self._all_conns, []
        for c in conns:
            try:
                c.close()
            except sqlite3.Error:
                pass

    # -- audit ------------------------------------------------------------
    def audit(self, op: str, ns: str, key: str, actor: str, detail: str = "",
              enabled: bool = True) -> None:
        if not enabled:
            return
        self.conn.execute(
            "INSERT INTO audit(ts,op,ns,key,actor,detail) VALUES (?,?,?,?,?,?)",
            (now_iso(), op, ns, key, actor, detail[:2000]),
        )

    # -- records ----------------------------------------------------------
    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        return {
            "ns": r["ns"],
            "key": r["key"],
            "type": r["type"],
            "value": r["value"],
            "tags": [t for t in r["tags"].split(",") if t],
            "actor": r["actor"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "version": r["version"],
            "bytes": len(r["value"].encode("utf-8")),
        }

    def get(self, ns: str, key: str) -> dict:
        r = self.conn.execute(
            "SELECT * FROM memory WHERE ns=? AND key=?", (ns, key)
        ).fetchone()
        if r is None:
            raise NotFound(f"no memory at {ns}/{key}")
        return self._row(r)

    def exists(self, ns: str, key: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM memory WHERE ns=? AND key=?", (ns, key)
        ).fetchone() is not None

    def put(self, ns: str, key: str, value: str, actor: str, tags: list[str] | None = None,
            mtype: str = "kv", audit_on: bool = True) -> dict:
        ts = now_iso()
        tagstr = ",".join(sorted({t.strip() for t in (tags or []) if t.strip()}))
        cur = self.conn.execute(
            """INSERT INTO memory(ns,key,type,value,tags,actor,created_at,updated_at,version)
               VALUES (?,?,?,?,?,?,?,?,1)
               ON CONFLICT(ns,key) DO UPDATE SET
                 value=excluded.value,
                 type=excluded.type,
                 tags=CASE WHEN excluded.tags='' THEN memory.tags ELSE excluded.tags END,
                 actor=excluded.actor,
                 updated_at=excluded.updated_at,
                 version=memory.version+1""",
            (ns, key, mtype, value, tagstr, actor, ts, ts),
        )
        self.audit("put", ns, key, actor, f"{len(value.encode('utf-8'))}B", audit_on)
        return self.get(ns, key)

    def append(self, ns: str, key: str, text: str, actor: str,
               audit_on: bool = True, sep: str = "\n") -> dict:
        ts = now_iso()
        if self.exists(ns, key):
            row = self.get(ns, key)
            if row["type"] == "log":
                new = (row["value"] + sep if row["value"] else "") + f"[{ts}] {text}"
            else:
                new = (row["value"] + sep if row["value"] else "") + text
            if len(new.encode("utf-8")) > 0:
                self.conn.execute(
                    """UPDATE memory SET value=?, actor=?, updated_at=?, version=version+1
                       WHERE ns=? AND key=?""",
                    (new, actor, ts, ns, key),
                )
            self.audit("append", ns, key, actor, f"+{len(text.encode('utf-8'))}B", audit_on)
            return self.get(ns, key)
        self.conn.execute(
            """INSERT INTO memory(ns,key,type,value,tags,actor,created_at,updated_at,version)
               VALUES (?,?,?,?,?,?,?, ?,1)""",
            (ns, key, "kv", text, "", actor, ts, ts),
        )
        self.audit("append", ns, key, actor, f"create {len(text.encode('utf-8'))}B", audit_on)
        return self.get(ns, key)

    def log(self, ns: str, key: str, text: str, actor: str, audit_on: bool = True) -> dict:
        """Append a timestamped line, creating a log-type record if absent."""
        if not self.exists(ns, key):
            self.conn.execute(
                """INSERT INTO memory(ns,key,type,value,tags,actor,created_at,updated_at,version)
                   VALUES (?,?, 'log', '', '', ?, ?, ?, 1)""",
                (ns, key, actor, now_iso(), now_iso()),
            )
        else:
            self.conn.execute("UPDATE memory SET type='log' WHERE ns=? AND key=?",
                              (ns, key))
        return self.append(ns, key, text, actor, audit_on)

    def delete(self, ns: str, key: str, actor: str, audit_on: bool = True) -> dict:
        row = self.get(ns, key)  # raises NotFound
        self.conn.execute("DELETE FROM memory WHERE ns=? AND key=?", (ns, key))
        self.audit("delete", ns, key, actor, f"was {row['bytes']}B", audit_on)
        return row

    def list(self, ns: str | None = None, limit: int = 100,
             tag: str | None = None, actor: str | None = None) -> list[dict]:
        sql = "SELECT * FROM memory"
        args: list = []
        conds = []
        if ns:
            conds.append("ns=?")
            args.append(ns)
        if tag:
            conds.append("(',' || tags || ',') LIKE ?")
            args.append(f"%,{tag},%")
        if actor:
            conds.append("actor=?")
            args.append(actor)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        return [self._row(r) for r in self.conn.execute(sql, args)]

    def audit_tail(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]

    def actors(self) -> list[dict]:
        return [
            {"actor": r["actor"], "count": r["c"], "last": r["last"]}
            for r in self.conn.execute(
                "SELECT actor, COUNT(*) c, MAX(updated_at) last "
                "FROM memory GROUP BY actor ORDER BY c DESC")
        ]

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]

    def search(self, query: str, ns: str | None = None, limit: int = 20) -> list[dict]:
        q = query.strip()
        if not q:
            return []
        candidates = [
            q,
            " ".join(f'"{t}"' for t in re.findall(r"[\w@./:-]+", q)),
            " OR ".join(f'"{t}"*' for t in re.findall(r"[\w@./:-]+", q)),
        ]
        rows: list[sqlite3.Row] = []
        last_err: Exception | None = None
        for cand in candidates:
            sql = (
                "SELECT m.* FROM memory_fts f JOIN memory m ON m.id = f.rowid "
                "WHERE memory_fts MATCH ?"
            )
            args: list = [cand]
            if ns:
                sql += " AND m.ns = ?"
                args.append(ns)
            sql += " ORDER BY rank LIMIT ?"
            args.append(limit)
            try:
                rows = list(self.conn.execute(sql, args))
                break
            except sqlite3.OperationalError as e:
                last_err = e
                continue
        if not rows and last_err is not None:
            # last resort: plain substring match so a query never just fails
            sql = "SELECT * FROM memory WHERE (value LIKE ? OR key LIKE ?)"
            args = [f"%{q}%", f"%{q}%"]
            if ns:
                sql += " AND ns=?"
                args.append(ns)
            sql += " ORDER BY updated_at DESC LIMIT ?"
            args.append(limit)
            rows = list(self.conn.execute(sql, args))
        return [self._row(r) for r in rows]

    def namespaces(self) -> list[dict]:
        return [
            {"ns": r["ns"], "count": r["c"]}
            for r in self.conn.execute(
                "SELECT ns, COUNT(*) c FROM memory GROUP BY ns ORDER BY ns"
            )
        ]

    # -- approvals --------------------------------------------------------
    def request_delete(self, ns: str, key: str, actor: str, ttl: int) -> dict:
        row = self.get(ns, key)  # raises NotFound
        token = secrets.token_urlsafe(9)
        ts = datetime.now(timezone.utc).replace(microsecond=0)
        expires = datetime.fromtimestamp(ts.timestamp() + ttl, tz=timezone.utc)
        self.conn.execute(
            """INSERT INTO approval(token,op,ns,key,snapshot,actor,requested_at,expires_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (token, "delete", ns, key, json.dumps(row, ensure_ascii=False),
             actor, ts.isoformat(), expires.isoformat()),
        )
        self.audit("delete_requested", ns, key, actor, f"token={token}")
        return {"token": token, "op": "delete", "ns": ns, "key": key,
                "requested_by": actor, "requested_at": ts.isoformat(),
                "expires_at": expires.isoformat(), "ttl_seconds": ttl,
                "approve_with": f"{PROG} approve {token}",
                "value_preview": row["value"][:300]}

    def approvals(self, state: str = "pending") -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT token,op,ns,key,actor,requested_at,expires_at FROM approval "
            "WHERE state=? ORDER BY id DESC", (state,))]

    def _take_approval(self, token: str, want: str) -> sqlite3.Row:
        r = self.conn.execute(
            "SELECT * FROM approval WHERE token=?", (token,)).fetchone()
        if r is None:
            raise NotFound(f"unknown approval token {token!r}")
        if r["state"] != "pending":
            raise UmemError(f"approval {token} was already {r['state']}")
        if want == "approve":
            exp = datetime.fromisoformat(r["expires_at"])
            if exp < datetime.now(timezone.utc):
                self.conn.execute("UPDATE approval SET state='expired' WHERE token=?",
                                  (token,))
                raise ApprovalRequired(f"approval {token} expired at {r['expires_at']}")
        return r

    def approve(self, token: str, human: str) -> dict:
        r = self._take_approval(token, "approve")
        self.conn.execute("UPDATE approval SET state='approved' WHERE token=?", (token,))
        row = self.delete(r["ns"], r["key"], f"human:{human}")
        self.audit("approve", r["ns"], r["key"], f"human:{human}", f"token={token}")
        return {"approved": True, "op": r["op"], "ns": r["ns"], "key": r["key"],
                "deleted": row}

    def deny(self, token: str, human: str) -> dict:
        r = self._take_approval(token, "deny")
        self.conn.execute("UPDATE approval SET state='denied' WHERE token=?", (token,))
        self.audit("deny", r["ns"], r["key"], f"human:{human}", f"token={token}")
        return {"approved": False, "op": r["op"], "ns": r["ns"], "key": r["key"]}


# --------------------------------------------------------------------------
# shared operation layer (used by CLI and MCP)
# --------------------------------------------------------------------------

class Engine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.store = Store(cfg["db"])
        self.actor = detect_actor()
        self.audit_on = bool(cfg.get("audit", True))

    def close(self) -> None:
        self.store.close()

    def _check_size(self, value: str) -> str:
        limit = int(self.cfg.get("max_value_bytes", 1_000_000))
        if len(value.encode("utf-8")) > limit:
            raise UmemError(
                f"value is {len(value.encode('utf-8'))}B, limit is {limit}B "
                f"(config: max_value_bytes)")
        return value

    def _ns(self, ns: str | None) -> str:
        return ns or self.cfg.get("default_ns", "global")

    def put(self, ns, key, value, tags=None, mtype="kv") -> dict:
        return self.store.put(self._ns(ns), key, self._check_size(value),
                              self.actor, tags, mtype, self.audit_on)

    def get(self, ns, key) -> dict:
        return self.store.get(self._ns(ns), key)

    def append(self, ns, key, text) -> dict:
        return self.store.append(self._ns(ns), key, self._check_size(text),
                                 self.actor, self.audit_on)

    def log(self, ns, key, text) -> dict:
        return self.store.log(self._ns(ns), key, self._check_size(text),
                              self.actor, self.audit_on)

    def list(self, ns=None, limit=100, tag=None, actor=None) -> list[dict]:
        return self.store.list(self._ns(ns) if ns else None, limit, tag, actor)

    def search(self, query, ns=None, limit=20) -> list[dict]:
        return self.store.search(query, self._ns(ns) if ns else None, limit)

    # -- delete: always human-gated --------------------------------------
    def delete_interactive(self, ns, key) -> dict:
        """TTY path: the human is right here, so ask them directly."""
        row = self.store.get(self._ns(ns), key)
        print(f"DELETE {row['ns']}/{row['key']} ({row['bytes']} bytes, "
              f"v{row['version']}, by {row['actor']})?", file=sys.stderr)
        preview = row["value"][:400]
        if preview:
            print(f"---\n{preview}\n---", file=sys.stderr)
        answer = input("Type 'yes' to permanently delete: ").strip().lower()
        if answer != "yes":
            self.store.audit("delete_aborted", row["ns"], row["key"], self.actor,
                             "human declined", self.audit_on)
            raise Denied("delete declined by human")
        deleted = self.store.delete(row["ns"], row["key"], self.actor, self.audit_on)
        return {"deleted": True, "ns": deleted["ns"], "key": deleted["key"],
                "record": deleted}

    def delete_request(self, ns, key) -> dict:
        """Non-interactive path: mint an approval token for a human to redeem."""
        ttl = int(self.cfg.get("approval_ttl_seconds", 900))
        return self.store.request_delete(self._ns(ns), key, self.actor, ttl)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def out(data, as_json: bool) -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    elif isinstance(data, str):
        print(data)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                ns = item.get("ns", "")
                key = item.get("key", "")
                val = (item.get("value") or "").replace("\n", "\n    ")
                print(f"{ns}/{key}  [{item.get('type','kv')}, {item.get('bytes',0)}B, "
                      f"{item.get('updated_at','')}]")
                if val:
                    print(f"    {val}")
            else:
                print(item)
    elif isinstance(data, dict):
        if "value" in data and "key" in data:
            print(data["value"])
        else:
            for k, v in data.items():
                print(f"{k}: {v}")
    else:
        print(data)


def read_value(args) -> str:
    if getattr(args, "stdin", False) or args.value == "-":
        return sys.stdin.read().rstrip("\n")
    if args.value is None:
        raise UmemError("no value given (pass it as an argument or with --stdin)")
    return args.value


def cmd_init(args, cfg) -> int:
    made_cfg = not CONFIG_PATH.exists()
    if made_cfg:
        cfg = json.loads(json.dumps(DEFAULTS))
        if _env("UNIVERSAL_MEMORY_DB", "UMEM_DB"):
            cfg["db"] = _env("UNIVERSAL_MEMORY_DB", "UMEM_DB")
        cfg["http"]["token"] = secrets.token_urlsafe(32)
        save_config(cfg)
    store = Store(cfg["db"])
    store.close()
    bin_path = pathlib.Path(sys.argv[0]).resolve()
    print(f"Universal Memory {VERSION} ready\n")
    print(f"  database : {cfg['db']}")
    print(f"  config   : {CONFIG_PATH}")
    print(f"  binary   : {bin_path}")
    print(f"  actor    : {detect_actor()}\n")
    print("Local agents (any shell):")
    print(f"  {bin_path} put hermes:notes \"hello\"")
    print(f"  {bin_path} get hermes:notes")
    print(f"  {bin_path} append hermes:notes \"more\"")
    print(f"  {bin_path} search hello")
    print(f"  {bin_path} del hermes:notes      # asks a human\n")
    print("stdio MCP (tools: universal_memory_*):")
    snippet = ('{"mcp":{"servers":{"universal":{"type":"local","command":["'
               + PROG + '","mcp"]}}}}')
    print(f"  {snippet}\n")
    print("HTTP MCP (browser agents):")
    print(f"  {bin_path} serve    # http://{cfg['http']['host']}:{cfg['http']['port']}/mcp")
    if cfg["http"].get("token"):
        print(f"  Authorization: Bearer {cfg['http']['token']}")
    return 0


def cmd_put(args, cfg, eng) -> int:
    value = read_value(args)
    tags = [t for t in (args.tags or "").split(",") if t]
    row = eng.put(args.ns, args.key, value, tags, args.type)
    out(row if args.json else f"stored {row['ns']}/{row['key']} "
        f"({row['bytes']}B, v{row['version']})", args.json)
    return 0


def cmd_get(args, cfg, eng) -> int:
    row = eng.get(args.ns, args.key)
    out(row if args.json else row["value"], args.json)
    return 0


def cmd_append(args, cfg, eng) -> int:
    text = read_value(args)
    row = eng.append(args.ns, args.key, text)
    out(row if args.json else f"appended to {row['ns']}/{row['key']} "
        f"({row['bytes']}B)", args.json)
    return 0


def cmd_log(args, cfg, eng) -> int:
    text = read_value(args)
    row = eng.log(args.ns, args.key, text)
    out(row if args.json else f"logged to {row['ns']}/{row['key']} "
        f"({row['bytes']}B)", args.json)
    return 0


def cmd_del(args, cfg, eng) -> int:
    if args.yes:
        # explicit human consent given on the command line
        deleted = eng.store.delete(eng._ns(args.ns), args.key, eng.actor, eng.audit_on)
        out({"deleted": True, "ns": deleted["ns"], "key": deleted["key"],
             "record": deleted} if args.json else
            f"deleted {deleted['ns']}/{deleted['key']}", args.json)
        return 0
    if sys.stdin.isatty() and sys.stdout.isatty():
        res = eng.delete_interactive(args.ns, args.key)
        out(res if args.json else f"deleted {res['ns']}/{res['key']}", args.json)
        return 0
    res = eng.delete_request(args.ns, args.key)
    msg = {
        "deleted": False,
        "approval_required": True,
        **res,
        "note": ("A human must approve this delete. Run this in a terminal: "
                 + res["approve_with"]),
    }
    out(msg, True)
    return ApprovalRequired.exit_code


def cmd_approve(args, cfg, eng) -> int:
    human = os.environ.get("USER", "unknown")
    res = eng.store.approve(args.token, human)
    out(res if args.json else
        f"approved: deleted {res['ns']}/{res['key']}", args.json)
    return 0


def cmd_deny(args, cfg, eng) -> int:
    human = os.environ.get("USER", "unknown")
    res = eng.store.deny(args.token, human)
    out(res if args.json else f"denied: kept {res['ns']}/{res['key']}", args.json)
    return 0


def cmd_pending(args, cfg, eng) -> int:
    rows = eng.store.approvals("pending")
    if args.json:
        out(rows, True)
    elif not rows:
        print("no pending approvals")
    else:
        for r in rows:
            print(f"{r['token']}  {r['ns']}/{r['key']}  "
                  f"requested by {r['actor']} at {r['requested_at']} "
                  f"(expires {r['expires_at']})")
            print(f"    approve: {PROG} approve {r['token']}   "
                  f"deny: {PROG} deny {r['token']}")
    return 0


def cmd_search(args, cfg, eng) -> int:
    rows = eng.search(args.query, args.ns, args.limit)
    if args.json:
        out(rows, True)
    elif not rows:
        print("no matches")
    else:
        out(rows, False)
    return 0


def cmd_list(args, cfg, eng) -> int:
    if args.namespaces:
        rows = eng.store.namespaces()
        if args.json:
            out(rows, True)
        else:
            for r in rows:
                print(f"{r['ns']}  ({r['count']})")
        return 0
    rows = eng.list(args.ns, args.limit, args.tag, args.actor)
    out(rows if args.json else (rows or "empty"), args.json)
    return 0


def cmd_whoami(args, cfg, eng) -> int:
    out({"actor": eng.actor, "db": cfg["db"], "config": str(CONFIG_PATH),
         "version": VERSION, "namespaces": eng.store.namespaces()}, True)
    return 0


def cmd_import(args, cfg, eng) -> int:
    text = pathlib.Path(args.file).expanduser().read_text()
    parts = [p.strip() for p in re.split(args.split_by, text) if p.strip()]
    n = 0
    for i, part in enumerate(parts):
        key = f"{pathlib.Path(args.file).stem}:{i:04d}"
        eng.put(args.ns, key, part, tags=import_tags(args) or None)
        n += 1
    out({"imported": n, "ns": eng._ns(args.ns), "file": args.file}
        if args.json else f"imported {n} entries into {eng._ns(args.ns)}", args.json)
    return 0


def import_tags(args) -> list[str]:
    return [t for t in (args.tags or "").split(",") if t]


def cmd_serve(args, cfg, eng) -> int:
    """MCP endpoint only — the thing that runs continuously (see README for a
    systemd user unit). The dashboard is opt-in via --dashboard."""
    host = args.host or cfg["http"]["host"]
    port = args.port or int(cfg["http"]["port"])
    return run_http_server(cfg, host, port, mcp=True,
                           dashboard=bool(getattr(args, "dashboard", False)))


def cmd_web(args, cfg, eng) -> int:
    """Dashboard + JSON API on their own port, started when wanted."""
    host = args.host or cfg["http"]["host"]
    port = args.port or int(cfg["http"].get("web_port")
                            or int(cfg["http"]["port"]) + 1)
    return run_http_server(cfg, host, port, dashboard=True,
                           mcp=bool(getattr(args, "mcp", False)))


def cmd_mcp(args, cfg, eng) -> int:
    return run_stdio_server(eng)


# --------------------------------------------------------------------------
# MCP protocol
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "memory_put",
        "description": (
            "Create or overwrite a memory record. Free to call - no approval. "
            "Use ns to scope the record (e.g. 'hermes', 'opencode', "
            "'project:myapp', 'shared')."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ns": {"type": "string", "description": "namespace (defaults to 'global')"},
                "key": {"type": "string", "description": "record key, unique inside ns"},
                "value": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "memory_get",
        "description": "Read one memory record by namespace + key.",
        "inputSchema": {
            "type": "object",
            "properties": {"ns": {"type": "string"}, "key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "memory_append",
        "description": ("Append text to an existing memory record (creates it if "
                        "missing). Free to call - no approval."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ns": {"type": "string"},
                "key": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["key", "text"],
        },
    },
    {
        "name": "memory_search",
        "description": ("Full-text search across every shared memory. Returns "
                        "matching records with their namespace and key."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "ns": {"type": "string", "description": "optional namespace filter"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_list",
        "description": "List recent memory records in a namespace (or all).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ns": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "memory_delete",
        "description": (
            "Request deletion of a memory record. This NEVER deletes anything by "
            "itself: it returns a short-lived approval token that only a human can "
            "redeem with `universal-memory approve <token>` at a terminal. Always "
            "call it and "
            "pass the token back to the user."),
        "inputSchema": {
            "type": "object",
            "properties": {"ns": {"type": "string"}, "key": {"type": "string"}},
            "required": ["key"],
        },
    },
]

MCP_TOOLS = {t["name"]: t for t in TOOLS}


def mcp_call(eng: Engine, name: str, args: dict) -> tuple[str, bool]:
    """Returns (text, is_error)."""
    try:
        if name == "memory_put":
            row = eng.put(args.get("ns"), args["key"], args["value"],
                          args.get("tags"), args.get("type", "kv"))
            return json.dumps({"ok": True, **row}, ensure_ascii=False, indent=2), False
        if name == "memory_get":
            row = eng.get(args.get("ns"), args["key"])
            return json.dumps(row, ensure_ascii=False, indent=2), False
        if name == "memory_append":
            row = eng.append(args.get("ns"), args["key"], args["text"])
            return json.dumps({"ok": True, **row}, ensure_ascii=False, indent=2), False
        if name == "memory_search":
            rows = eng.search(args["query"], args.get("ns"), int(args.get("limit", 20)))
            return json.dumps({"count": len(rows), "results": rows},
                              ensure_ascii=False, indent=2), False
        if name == "memory_list":
            rows = eng.list(args.get("ns"), int(args.get("limit", 50)))
            return json.dumps({"count": len(rows), "results": rows},
                              ensure_ascii=False, indent=2), False
        if name == "memory_delete":
            res = eng.delete_request(args.get("ns"), args["key"])
            res["deleted"] = False
            res["approval_required"] = True
            res["note"] = ("Deletion needs human approval. Give this token to the "
                           "user and tell them to run: " + res["approve_with"])
            return json.dumps(res, ensure_ascii=False, indent=2), False
        return f"unknown tool: {name}", True
    except NotFound as e:
        return json.dumps({"error": str(e), "code": "not_found"}), True
    except UmemError as e:
        return json.dumps({"error": str(e), "code": type(e).__name__}), True
    except Exception as e:  # noqa: BLE001 - never crash the protocol loop
        return json.dumps({"error": f"{type(e).__name__}: {e}"}), True


def mcp_handle(eng: Engine, msg: dict, protocol_hint: str) -> dict | None:
    """Handle one JSON-RPC message. Returns None for notifications."""
    mid = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

    if method.startswith("notifications/"):
        return None

    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", protocol_hint),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": VERSION},
            "instructions": (
                "Universal agent memory. memory_put / memory_get / memory_append / "
                "memory_search / memory_list are free to use. memory_delete only "
                "requests an approval token - a human must run `universal-memory "
                "approve "
                "<token>`; never claim a delete succeeded until the user confirms."),
        }
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        tool = MCP_TOOLS.get(name)
        if tool is None:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown tool {name}"}}
        text, is_err = mcp_call(eng, name, args)
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"content": [{"type": "text", "text": text}],
                           "isError": is_err}}

    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def _dispatch(eng: Engine, payload, protocol_hint: str):
    if isinstance(payload, list):
        replies = [mcp_handle(eng, m, protocol_hint) for m in payload]
        replies = [r for r in replies if r is not None]
        return replies if replies else None
    return mcp_handle(eng, payload, protocol_hint)


def run_stdio_server(eng: Engine) -> int:
    """MCP over stdio: newline-delimited JSON-RPC."""
    protocol_hint = "2025-03-26"
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "parse error"}}
            print(json.dumps(resp), flush=True)
            continue
        if isinstance(payload, dict) and payload.get("method") == "initialize":
            protocol_hint = (payload.get("params") or {}).get(
                "protocolVersion", protocol_hint)
        reply = _dispatch(eng, payload, protocol_hint)
        if reply is None:
            continue
        if isinstance(reply, list):
            for r in reply:
                print(json.dumps(r), flush=True)
        else:
            print(json.dumps(reply), flush=True)
    return 0


# --------------------------------------------------------------------------
# agent discovery - what is installed on this machine, and is it connected?
# --------------------------------------------------------------------------

HOME = pathlib.Path.home()


def _p(path: str) -> str:
    return str(pathlib.Path(path).expanduser())


# `connect` is only set for runtimes whose recipe is verified on this machine.
# Everything else exposes a manual command instead of guessing CLI flags.
AGENTS: list[dict] = [
    {
        "id": "hermes", "ns": "hermes", "label": "Hermes Agent",
        "bins": ["hermes"], "configs": ["~/.hermes/config.yaml"],
        "version_cmd": ["hermes", "--version"],
        "manual": ("add to ~/.hermes/config.yaml:\n\n"
                   "mcp_servers:\n  universal:\n    command: universal-memory\n"
                   '    args: ["mcp"]'),
        "connect": {"kind": "yaml", "file": "~/.hermes/config.yaml"},
    },
    {
        "id": "opencode", "ns": "opencode", "label": "OpenCode",
        "bins": ["opencode"], "configs": ["~/.config/opencode/opencode.json"],
        "version_cmd": ["opencode", "--version"],
        "manual": "opencode mcp add universal --global -- universal-memory mcp",
        "connect": {"kind": "cmd",
                    "cmd": ["opencode", "mcp", "add", "universal", "--global",
                            "--", "universal-memory", "mcp"]},
    },
    {
        "id": "claude-code", "ns": "claude-code", "label": "Claude Code",
        "bins": ["claude"], "configs": ["~/.claude.json", "~/.claude/settings.json"],
        "version_cmd": ["claude", "--version"],
        "manual": "claude mcp add --scope user universal -- universal-memory mcp",
        "connect": None,
    },
    {
        "id": "codex", "ns": "codex", "label": "Codex CLI",
        "bins": ["codex"], "configs": ["~/.codex/config.toml"],
        "version_cmd": ["codex", "--version"],
        "manual": ('add to ~/.codex/config.toml:\n\n[mcp_servers.universal]\n'
                   'command = "universal-memory"\nargs = ["mcp"]'),
        "connect": None,
    },
    {
        "id": "cursor", "ns": "cursor", "label": "Cursor Agent",
        "bins": ["cursor-agent"], "configs": ["~/.cursor/mcp.json"],
        "version_cmd": ["cursor-agent", "--version"],
        "manual": ('add to ~/.cursor/mcp.json under "mcpServers":\n\n'
                   '"universal": {"command": ["universal-memory", "mcp"]}'),
        "connect": None,
    },
    {
        "id": "gemini-cli", "ns": "gemini-cli", "label": "Gemini CLI",
        "bins": ["gemini"], "configs": ["~/.gemini/settings.json"],
        "version_cmd": ["gemini", "--version"],
        "manual": ('add to ~/.gemini/settings.json under "mcpServers":\n\n'
                   '"universal": {"command": ["universal-memory", "mcp"]}'),
        "connect": None,
    },
    {
        "id": "aider", "ns": "aider", "label": "Aider",
        "bins": ["aider"], "configs": ["~/.aider.conf.yml"],
        "version_cmd": ["aider", "--version"],
        "manual": "Aider has no MCP client - use the universal-memory CLI directly.",
        "connect": None,
    },
]


def _version_of(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    if not lines:
        return None
    return re.sub(r"\x1b\[[0-9;]*m", "", lines[0]).strip()[:120]


def _connected_in(path: str) -> bool:
    try:
        return "universal-memory" in pathlib.Path(path).read_text(errors="replace")
    except OSError:
        return False


def detect_agents() -> list[dict]:
    """Scan PATH and config dirs for known agent runtimes."""
    found = []
    for a in AGENTS:
        bins = [b for b in a["bins"] if shutil.which(b)]
        existing = [c for c in (_p(c) for c in a["configs"])
                    if pathlib.Path(c).exists()]
        found.append({
            "id": a["id"], "ns": a["ns"], "label": a["label"],
            "installed": bool(bins) or bool(existing),
            "bin": bins[0] if bins else None,
            "version": _version_of(a["version_cmd"]) if bins else None,
            "config_files": existing,
            "connected": any(_connected_in(c) for c in existing),
            "auto_connect": a["connect"] is not None,
            "manual": a["manual"],
        })
    return found


def _validate_config(path: pathlib.Path, text: str) -> str | None:
    """Return an error string if the patched config would not parse."""
    suffix = path.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            import yaml
            data = yaml.safe_load(text)
            if not isinstance(data, dict):
                return "patched YAML is not a mapping"
        elif suffix == ".json":
            json.loads(text)
        elif suffix == ".toml":
            import tomllib
            tomllib.loads(text)
    except Exception as e:  # noqa: BLE001 - any parse failure blocks the write
        return f"{type(e).__name__}: {e}"
    return None


def _yaml_patch(text: str) -> str:
    if "universal-memory" in text:
        return text
    m = re.search(r"^mcp_servers:[ \t]*\n", text, re.M)
    block = ('  universal:\n    command: universal-memory\n'
             '    args: ["mcp"]\n')
    if m:
        return text[:m.end()] + block + text[m.end():]
    return text.rstrip("\n") + "\n\nmcp_servers:\n" + block


def _patch_config_file(path_str: str, patcher, manual: str) -> dict:
    p = pathlib.Path(_p(path_str))
    original = p.read_text() if p.exists() else ""
    try:
        new = patcher(original)
    except Exception as e:  # noqa: BLE001
        return {"connected": False, "error": f"patch failed: {e}", "manual": manual}
    if new == original:
        return {"connected": True, "output": "already connected"}
    err = _validate_config(p, new)
    if err:
        return {"connected": False,
                "error": f"refusing to write, patched config would not parse ({err})",
                "manual": manual}
    backup = p.with_name(p.name + ".bak")
    try:
        if original:
            backup.write_text(original)
        p.write_text(new)
    except OSError as e:
        return {"connected": False, "error": str(e), "manual": manual}
    if _validate_config(p, p.read_text()) is None:
        return {"connected": True,
                "output": f"patched {p}" + (f" (backup: {backup.name})" if original else "")}
    p.write_text(original)  # roll back
    return {"connected": False,
            "error": "validation failed after write, rolled back", "manual": manual}


def connect_agent(agent_id: str) -> dict:
    spec = next((a for a in AGENTS if a["id"] == agent_id), None)
    if spec is None:
        raise NotFound(f"unknown agent {agent_id!r}")
    recipe = spec.get("connect")
    if not recipe:
        return {"connected": False, "manual": spec["manual"],
                "note": "no verified auto-connect recipe for this runtime; "
                        "apply the command above in a terminal."}
    if recipe["kind"] == "cmd":
        try:
            r = subprocess.run(recipe["cmd"], capture_output=True, text=True,
                               timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            return {"connected": False, "error": str(e), "manual": spec["manual"]}
        text = ((r.stdout or "") + (r.stderr or "")).strip()[:2000]
        return {"connected": r.returncode == 0, "returncode": r.returncode,
                "output": text}
    if recipe["kind"] == "yaml":
        return _patch_config_file(recipe["file"], _yaml_patch, spec["manual"])
    return {"connected": False, "error": "unsupported recipe", "manual": spec["manual"]}


# --------------------------------------------------------------------------
# overlap - what have different agents stored as the same thing?
# --------------------------------------------------------------------------

def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[\w']+", text.lower()) if len(w) > 2}


def compute_overlap(store, max_records: int = 600,
                    threshold: float = 0.72) -> list[dict]:
    rows = [r for r in store.list(None, max_records) if 0 < r["bytes"] <= 4000]
    clusters: list[dict] = []
    used: set[tuple] = set()

    def add(reason: str, score: float | None, grp: list[dict]) -> None:
        namespaces = {r["ns"] for r in grp}
        if len(namespaces) < 2:
            return
        ids = frozenset((r["ns"], r["key"]) for r in grp)
        if ids in used:
            return
        used.add(ids)
        clusters.append({
            "reason": reason,
            "score": score,
            "namespaces": sorted(namespaces),
            "preview": grp[0]["value"][:300],
            "records": [{"ns": r["ns"], "key": r["key"], "actor": r["actor"],
                         "updated_at": r["updated_at"], "bytes": r["bytes"],
                         "type": r["type"]} for r in grp],
        })

    # 1. identical content (ignoring case / whitespace)
    exact: dict[str, list] = {}
    for r in rows:
        norm = re.sub(r"\s+", " ", r["value"].strip().lower())
        if len(norm) < 12:
            continue
        exact.setdefault(hashlib.sha1(norm.encode()).hexdigest(), []).append(r)
    for grp in exact.values():
        add("identical content", 1.0, grp)

    # 2. the same key held in different namespaces
    bykey: dict[str, list] = {}
    for r in rows:
        bykey.setdefault(r["key"], []).append(r)
    for grp in bykey.values():
        add("same key", None, grp)

    # 3. near-identical wording across namespaces
    pool = [(r, _words(r["value"])) for r in rows if len(r["value"]) >= 12]
    for i, (a, wa) in enumerate(pool):
        if not wa or (a["ns"], a["key"]) in used:
            continue
        for b, wb in pool[i + 1:]:
            if a["ns"] == b["ns"] or (b["ns"], b["key"]) in used or not wb:
                continue
            if abs(len(wa) - len(wb)) / max(len(wa), len(wb)) > 0.4:
                continue
            inter = len(wa & wb)
            union = len(wa | wb)
            if union and inter / union >= threshold:
                add("very similar wording", round(inter / union, 2), [a, b])

    clusters.sort(key=lambda c: (-(c["score"] if c["score"] is not None else -1),
                                 -len(c["records"])))
    return clusters


WEB_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Universal Memory</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--fg:#e6edf3;--dim:#8b949e;--acc:#58a6ff;--ok:#3fb950;--warn:#d29922;--bad:#f85149}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{display:flex;align-items:center;gap:16px;padding:14px 20px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:5}
h1{font-size:16px;margin:0}
h1 span{color:var(--acc)}
.sub{color:var(--dim);font-size:12.5px}
nav{display:flex;gap:4px;padding:8px 20px 0;border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
nav button{background:none;border:0;border-bottom:2px solid transparent;color:var(--dim);padding:9px 13px;cursor:pointer;font-size:13.5px}
nav button.on{color:var(--fg);border-bottom-color:var(--acc)}
main{padding:20px;max-width:1240px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px}
.card .n{font-size:26px;font-weight:600}
.card .l{color:var(--dim);font-size:11.5px;text-transform:uppercase;letter-spacing:.6px}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:13px;vertical-align:top}
th{color:var(--dim);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.5px}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:#1c2129}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.dim{color:var(--dim)}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;border:1px solid var(--line);white-space:nowrap}
.yes{color:var(--ok);border-color:#238636}
.no{color:var(--bad);border-color:#8b2b2b}
.warn{color:var(--warn)}
input,select{background:#0d1117;border:1px solid var(--line);color:var(--fg);padding:7px 9px;border-radius:6px;font-size:13px}
input:focus,select:focus{outline:none;border-color:var(--acc)}
.filters{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
.filters input{min-width:260px;flex:1}
button.act{background:#21262d;border:1px solid var(--line);color:var(--fg);padding:6px 12px;border-radius:6px;cursor:pointer;font-size:13px}
button.act:hover{border-color:var(--acc);color:var(--acc)}
button.primary{background:#238636;border-color:#238636;color:#fff}
button.primary:hover{background:#2ea043;color:#fff}
button.danger{background:#21262d;border-color:#8b2b2b;color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:14px}
.agent{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px}
.agent h3{margin:0 0 2px;font-size:15px}
.agent .meta{font-size:12px;color:var(--dim);margin-bottom:10px;word-break:break-all}
.agent .meta b{color:var(--fg);font-weight:500}
pre{background:#0d1117;border:1px solid var(--line);border-radius:6px;padding:10px;overflow:auto;font-size:12.5px;margin:8px 0;white-space:pre-wrap;word-break:break-word}
.drawer{position:fixed;top:0;right:0;height:100%;width:min(640px,100%);background:var(--panel);border-left:1px solid var(--line);padding:20px;overflow:auto;transform:translateX(100%);transition:.18s;z-index:20}
.drawer.open{transform:none}
.drawer h2{margin-top:0;font-size:15px;word-break:break-all}
.kv{display:grid;grid-template-columns:125px 1fr;gap:6px 10px;font-size:13px;margin-bottom:14px}
.kv div:nth-child(odd){color:var(--dim)}
.value{white-space:pre-wrap;word-break:break-word;background:#0d1117;border:1px solid var(--line);border-radius:6px;padding:12px}
.row{cursor:pointer}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#1c2129;border:1px solid var(--line);padding:10px 16px;border-radius:8px;z-index:40;display:none;max-width:80%}
.empty{color:var(--dim);padding:26px;text-align:center}
h2.sec{font-size:14px;text-transform:uppercase;letter-spacing:.7px;color:var(--dim);margin:22px 0 10px}
h2.sec:first-child{margin-top:0}
#lock{max-width:560px;margin:70px auto;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:24px}
#lock code{background:#0d1117;padding:2px 6px;border-radius:4px}
.clu{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px;margin-bottom:12px}
.clu .why{font-size:12px;color:var(--warn);margin-bottom:6px}
</style>
</head>
<body>
<header>
  <h1>Universal <span>Memory</span></h1>
  <div class="sub" id="summary">loading…</div>
  <div style="margin-left:auto"><button class="act" onclick="refresh()">Refresh</button></div>
</header>
<nav id="nav">
  <button class="on" data-v="overview">Overview</button>
  <button data-v="records">Records</button>
  <button data-v="agents">Agents</button>
  <button data-v="shared">Shared facts</button>
  <button data-v="activity">Activity</button>
  <button data-v="approvals">Approvals</button>
</nav>
<main id="main"></main>
<div class="drawer" id="drawer"></div>
<div class="toast" id="toast"></div>
<script>
const TOKEN=(()=>{const u=new URL(location.href);const t=u.searchParams.get('t');
if(t){sessionStorage.setItem('um_t',t);u.searchParams.delete('t');history.replaceState(null,'',u.pathname+u.search);}
return sessionStorage.getItem('um_t')||'';})();

let V='overview', F={q:'',ns:'',actor:''}, DATA={};

function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.style.display='block';clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',4000);}
async function api(path,opts){
  opts=opts||{};
  const r=await fetch(path,{method:opts.method||'GET',body:opts.body,
    headers:Object.assign({'X-UMem-Token':TOKEN},opts.body?{'Content-Type':'application/json'}:{})});
  if(r.status===401){sessionStorage.removeItem('um_t');location.reload();throw new Error('unauthorized');}
  let j=null;try{j=await r.json();}catch(e){}
  if(!r.ok){const m=(j&&j.error)||r.statusText;toast(r.status+': '+m);throw new Error(m);}
  return j;
}
function ago(iso){if(!iso)return '—';
  const d=(Date.now()-new Date(iso).getTime())/1000;
  if(d<60)return Math.max(0,Math.round(d))+'s ago';
  if(d<3600)return Math.round(d/60)+'m ago';
  if(d<86400)return Math.round(d/3600)+'h ago';
  return Math.round(d/86400)+'d ago';}
function short(s,n){s=String(s||'').replace(/\s+/g,' ').trim();return s.length>(n||90)?s.slice(0,n||90)+'…':s;}

document.getElementById('nav').addEventListener('click',e=>{
  const b=e.target.closest('button');if(!b)return;
  V=b.dataset.v;
  document.querySelectorAll('#nav button').forEach(x=>x.classList.toggle('on',x===b));
  render();
});

/* ---------------- views ---------------- */
async function render(){
  const m=document.getElementById('main');
  try{
    if(V==='overview'){
      const s=await api('/api/stats');DATA.stats=s;
      document.getElementById('summary').textContent=
        s.records+' records · '+s.namespaces.length+' namespaces · '+
        s.agents.filter(a=>a.installed).length+' agents installed · '+
        s.agents.filter(a=>a.connected).length+' connected';
      m.innerHTML=
        '<div class="cards">'+
        card(s.records,'records')+card(s.namespaces.length,'namespaces')+
        card(s.actors.length,'writing agents')+card(s.agents.filter(a=>a.installed).length,'agents installed')+
        card(s.agents.filter(a=>a.connected).length,'connected')+
        card(s.pending,'pending deletes',s.pending?'warn':'')+'</div>'+
        '<h2 class="sec">Namespaces <span class="dim" style="text-transform:none;letter-spacing:0">'+
        '— where a memory is scoped</span></h2>'+nsTable(s.namespaces)+
        '<h2 class="sec">Who is writing <span class="dim" style="text-transform:none;letter-spacing:0">'+
        '— the agent that stored it</span></h2>'+actorTable(s.actors)+
        '<div class="dim" style="font-size:12.5px;margin-top:8px">'+
        'Namespaces and agents are different things: a namespace is a bucket, '+
        'a writer is whoever filled it. Installed runtimes live on the '+
        '<b>Agents</b> tab — one only appears above after it writes its first memory.</div>'+
        '<h2 class="sec">Recent writes</h2>'+recTable(s.recent);
    }
    else if(V==='records'){
      const ns=await api('/api/namespaces');
      const q=new URLSearchParams({limit:'250'});
      if(F.ns)q.set('ns',F.ns);if(F.actor)q.set('actor',F.actor);if(F.q)q.set('q',F.q);
      const r=await api('/api/records?'+q);
      DATA.records=r.results;
      m.innerHTML='<div class="filters">'+
        '<input id="fq" placeholder="full-text search…" value="'+esc(F.q)+'">'+
        sel('fns','all namespaces',ns.map(x=>x.ns),F.ns)+
        sel('fact','all agents',r.actors,F.actor)+
        '<button class="act" onclick="doSearch()">Search</button>'+
        (F.q||F.ns||F.actor?'<button class="act" onclick="clearF()">Clear</button>':'')+
        '</div><div class="dim" style="margin-bottom:8px">'+r.results.length+
        ' record'+(r.results.length===1?'':'s')+(F.q?' matching “'+esc(F.q)+'”':'')+'</div>'+
        recTable(r.results);
      document.getElementById('fq').addEventListener('keydown',e=>{if(e.key==='Enter')doSearch();});
      document.getElementById('fns').onchange=e=>{F.ns=e.target.value;render();};
      document.getElementById('fact').onchange=e=>{F.actor=e.target.value;render();};
    }
    else if(V==='agents'){
      const a=await api('/api/agents');
      m.innerHTML='<div class="dim" style="margin-bottom:14px">Detected by scanning <code>PATH</code> and known config directories. '+
        '“Connected” means universal-memory is already in that runtime’s MCP config.</div><div class="grid">'+
        a.map(agentCard).join('')+'</div>';
      m.querySelectorAll('[data-connect]').forEach(b=>b.onclick=()=>connectAgent(b.dataset.connect));
      m.querySelectorAll('[data-ns]').forEach(b=>b.onclick=()=>{F={q:'',ns:b.dataset.ns,actor:''};go('records');});
    }
    else if(V==='shared'){
      const c=await api('/api/overlap');
      m.innerHTML='<div class="dim" style="margin-bottom:14px">The same information stored by more than one agent — '+
        'identical text, the same key, or very similar wording.</div>'+
        (c.length?c.map(cluCard).join(''):'<div class="empty">Nothing shared across agents yet.</div>');
      m.querySelectorAll('[data-rec]').forEach(el=>el.onclick=()=>openRec(el.dataset.ns,el.dataset.key));
    }
    else if(V==='activity'){
      const a=await api('/api/audit?limit=200');
      m.innerHTML=a.length?'<table><thead><tr><th>when</th><th>op</th><th>record</th><th>agent</th><th>detail</th></tr></thead><tbody>'+
        a.map(x=>'<tr><td class="dim">'+esc(ago(x.ts))+'</td><td>'+op(x.op)+'</td>'+
        '<td class="mono">'+esc(x.ns)+'/'+esc(x.key)+'</td><td class="mono dim">'+esc(x.actor)+'</td>'+
        '<td class="dim">'+esc(x.detail)+'</td></tr>').join('')+'</tbody></table>'
        :'<div class="empty">No activity recorded yet.</div>';
    }
    else if(V==='approvals'){
      const a=await api('/api/approvals');
      m.innerHTML='<div class="dim" style="margin-bottom:14px">Deletion is the one gated operation. '+
        'Only a human can approve or deny — approving here is your consent, recorded in the audit trail as <code>human:&lt;you&gt;</code>.</div>'+
        (a.length?'<table><thead><tr><th>record</th><th>requested by</th><th>expires</th><th></th></tr></thead><tbody>'+
        a.map(x=>{
          const live=!x.expired;
          return '<tr><td class="mono">'+esc(x.ns)+'/'+esc(x.key)+'</td><td class="mono dim">'+esc(x.actor)+'</td>'+
          '<td class="dim">'+(live?esc(ago(x.expires_at)):'<span class="pill warn">expired</span>')+'</td>'+
          '<td style="text-align:right">'+
          (live?
            '<button class="act primary" onclick="decide(\''+esc(x.token)+'\',true)">Approve</button> '+
            '<button class="act danger" onclick="decide(\''+esc(x.token)+'\',false)">Deny</button>'
            :'<span class="dim" style="font-size:12.5px">token expired — request the delete again</span>')+
          '</td></tr>';}).join('')+'</tbody></table>'
        :'<div class="empty">No deletions waiting for you.</div>');
    }
  }catch(e){m.innerHTML='<div class="empty">'+esc(e.message)+'</div>';}
}
function card(n,l,cls){return '<div class="card"><div class="n '+(cls||'')+'">'+n+'</div><div class="l">'+l+'</div></div>';}
function sel(id,all,items,cur){
  return '<select id="'+id+'"><option value="">'+all+'</option>'+
    items.map(x=>'<option value="'+esc(x)+'"'+(x===cur?' selected':'')+'>'+esc(x)+'</option>').join('')+'</select>';
}
function op(o){const c=o==='delete'||o==='delete_aborted'?'no':(o==='approve'?'yes':'');return '<span class="pill '+c+'">'+esc(o)+'</span>';}
function nsTable(rows){
  if(!rows.length)return '<div class="empty">empty</div>';
  return '<table><thead><tr><th>namespace</th><th>what it is</th><th>records</th><th></th></tr></thead><tbody>'+
    rows.map(r=>'<tr class="row" onclick="F={q:\'\',ns:\''+esc(r.ns)+'\',actor:\'\'};go(\'records\')">'+
    '<td class="mono">'+esc(r.ns)+'</td>'+
    '<td class="dim">'+(r.about?esc(r.about):'<span class="dim">custom namespace</span>')+'</td>'+
    '<td>'+r.count+'</td>'+
    '<td class="dim">open →</td></tr>').join('')+'</tbody></table>';
}
function actorTable(rows){
  if(!rows.length)return '<div class="empty">no writers yet</div>';
  return '<table><thead><tr><th>agent (who wrote it)</th><th>wrote</th><th>last write</th></tr></thead><tbody>'+
    rows.map(r=>'<tr class="row" onclick="F={q:\'\',ns:\'\',actor:\''+esc(r.actor)+'\'};go(\'records\')">'+
    '<td class="mono">'+esc(r.actor)+'</td><td>'+r.count+'</td><td class="dim">'+esc(ago(r.last))+'</td></tr>').join('')+'</tbody></table>';
}
function recTable(rows){
  if(!rows.length)return '<div class="empty">Nothing here yet.</div>';
  return '<table><thead><tr><th>namespace</th><th>key</th><th>content</th><th>agent</th><th>updated</th></tr></thead><tbody>'+
    rows.map(r=>'<tr class="row" onclick="openRec(\''+esc(r.ns)+'\',\''+esc(r.key)+'\')">'+
    '<td class="mono">'+esc(r.ns)+'</td><td class="mono">'+esc(r.key)+'</td>'+
    '<td class="dim">'+esc(short(r.value,70))+'</td><td class="mono dim">'+esc(short(r.actor,34))+'</td>'+
    '<td class="dim">'+esc(ago(r.updated_at))+'</td></tr>').join('')+'</tbody></table>';
}
function agentCard(a){
  const inst=a.installed, conn=a.connected;
  return '<div class="agent"><h3>'+esc(a.label)+'</h3>'+
    '<div class="meta">'+(inst?'<b>'+esc(a.version||a.bin||'installed')+'</b>':'not installed')+
    (a.config_files.length?'<br>'+esc(a.config_files.join(', ')):'')+'</div>'+
    '<div style="margin-bottom:12px">'+
      '<span class="pill '+(inst?'yes':'no')+'">'+(inst?'installed':'not installed')+'</span> '+
      '<span class="pill '+(conn?'yes':'no')+'">'+(conn?'connected':'not connected')+'</span>'+
    '</div>'+
    (inst&&!conn&&a.auto_connect?'<button class="act primary" data-connect="'+esc(a.id)+'">Connect to Universal Memory</button> ':'')+
    (inst&&!conn&&!a.auto_connect?'<div class="dim" style="font-size:12.5px;margin-bottom:6px">Run this to connect:</div><pre>'+esc(a.manual)+'</pre>':'')+
    (inst?'<button class="act" data-ns="'+esc(a.ns)+'">View its memory</button>':'')+
    (a.connectError?'<div class="pill no" style="margin-top:8px">'+esc(a.connectError)+'</div>':'')+
    (a.connectOutput?'<pre>'+esc(a.connectOutput)+'</pre>':'')+
    '</div>';
}
function cluCard(c){
  return '<div class="clu"><div class="why">'+esc(c.reason)+
    (c.score!=null?' · similarity '+Math.round(c.score*100)+'%':'')+
    ' · '+c.namespaces.length+' namespaces</div>'+
    '<div style="margin-bottom:8px">'+esc(short(c.preview,180))+'</div>'+
    '<table><thead><tr><th>namespace</th><th>key</th><th>agent</th><th>updated</th></tr></thead><tbody>'+
    c.records.map(r=>'<tr class="row" data-rec="1" data-ns="'+esc(r.ns)+'" data-key="'+esc(r.key)+'">'+
    '<td class="mono">'+esc(r.ns)+'</td><td class="mono">'+esc(r.key)+'</td>'+
    '<td class="mono dim">'+esc(short(r.actor,34))+'</td><td class="dim">'+esc(ago(r.updated_at))+'</td></tr>').join('')+
    '</tbody></table></div>';
}
function go(v){V=v;document.querySelectorAll('#nav button').forEach(x=>x.classList.toggle('on',x.dataset.v===v));render();}
function doSearch(){F.q=document.getElementById('fq').value.trim();render();}
function clearF(){F={q:'',ns:'',actor:''};render();}

async function openRec(ns,key){
  try{
    const r=await api('/api/record?ns='+encodeURIComponent(ns)+'&key='+encodeURIComponent(key));
    const d=document.getElementById('drawer');
    d.innerHTML='<div style="display:flex;gap:8px;margin-bottom:8px">'+
      '<button class="act" onclick="document.getElementById(\'drawer\').classList.remove(\'open\')">← Close</button>'+
      '<button class="act" style="margin-left:auto" onclick="copyText(this)" data-copy="'+esc(r.value)+'">Copy value</button></div>'+
      '<h2>'+esc(r.ns)+' / '+esc(r.key)+'</h2>'+
      '<div class="kv"><div>type</div><div>'+esc(r.type)+'</div>'+
      '<div>size</div><div>'+r.bytes+' bytes · version '+r.version+'</div>'+
      '<div>tags</div><div>'+(r.tags.length?r.tags.map(t=>'<span class="pill">'+esc(t)+'</span>').join(' '):'<span class="dim">none</span>')+'</div>'+
      '<div>written by</div><div class="mono">'+esc(r.actor)+'</div>'+
      '<div>created</div><div>'+esc(r.created_at)+'</div>'+
      '<div>updated</div><div>'+esc(r.updated_at)+' ('+esc(ago(r.updated_at))+')</div></div>'+
      '<div class="value">'+esc(r.value)+'</div>'+
      '<div style="margin-top:14px"><button class="act danger" onclick="requestDelete(\''+esc(r.ns)+'\',\''+esc(r.key)+'\')">'+
      'Request delete…</button><div class="hint">Asks for human approval — nothing is removed until you approve it.</div><div id="delout"></div></div>';
    d.classList.add('open');
  }catch(e){}
}
function copyText(b){
  navigator.clipboard.writeText(b.dataset.copy||'').then(()=>toast('value copied'));
}
async function requestDelete(ns,key){
  if(!confirm('Request deletion of '+ns+'/'+key+' ?'))return;
  try{
    const r=await api('/api/delete-request',{method:'POST',body:JSON.stringify({ns:ns,key:key})});
    document.getElementById('delout').innerHTML='<pre>NOT deleted — approval required.\n\n'+
      'token: '+esc(r.token)+'\nexpires: '+esc(r.expires_at)+'\n\n'+
      'redeem it at a terminal:\n  universal-memory approve '+esc(r.token)+'</pre>';
  }catch(e){}
}
async function decide(token,approve){
  const what=approve?'DELETE this record?':'keep this record?';
  if(!confirm(what+' This is your approval, recorded in the audit trail.'))return;
  try{
    await api(approve?'/api/approve':'/api/deny',{method:'POST',body:JSON.stringify({token:token})});
    toast(approve?'deleted':'kept');render();
  }catch(e){}
}
async function connectAgent(id){
  try{
    const r=await api('/api/agents/'+encodeURIComponent(id)+'/connect',{method:'POST',body:'{}'});
    toast(r.connected?('connected '+id):('connect failed: '+(r.error||'see manual command')));
    if(!r.connected&&!r.error)r.manual&&toast('apply manually: see card');
    render();
  }catch(e){}
}
async function refresh(){render();}
if(!TOKEN){
  document.getElementById('main').innerHTML='<div id="lock"><h2 style="margin-top:0">Token required</h2>'+
  '<p class="dim">This dashboard is protected by the same token as the MCP endpoint. Open it with:</p>'+
  '<pre>universal-memory web</pre><p class="dim">then follow the printed URL (<code>?t=…</code>).</p></div>';
  document.getElementById('summary').textContent='not authenticated';
}else{render();}
</script>
</body></html>
"""


def run_http_server(cfg: dict, host: str, port: int,
                    mcp: bool = True, dashboard: bool = False) -> int:
    """Serve whichever surfaces the caller asked for: the MCP Streamable HTTP
    endpoint (`serve`) and/or the dashboard + JSON API (`web`)."""
    surfaces = [s for s, on in (("mcp", mcp), ("dashboard", dashboard)) if on]
    engine = Engine(cfg)
    token = cfg["http"].get("token", "")
    if not token:
        token = secrets.token_urlsafe(32)
        cfg = json.loads(json.dumps(cfg))
        cfg["http"]["token"] = token
        save_config(cfg)
        print("generated HTTP token in config", file=sys.stderr)

    protocol_lock = threading.Lock()
    state = {"protocolHint": "2025-03-26"}

    class Handler(BaseHTTPRequestHandler):
        server_version = f"universal-memory/{VERSION}"

        def log_message(self, fmt, *a):
            # never log the query string: it can carry ?t=<token>
            sys.stderr.write("%s - %s\n" % (
                self.address_string(), re.sub(r"\?\S*", "?…", fmt % a)))

        def _qs(self) -> dict:
            return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type, Authorization, X-UMem-Token, "
                             "Mcp-Session-Id")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                       "application/json")

        def _authed(self) -> bool:
            hdr = self.headers.get("Authorization", "")
            supplied = hdr[7:] if hdr.startswith("Bearer ") else \
                self.headers.get("X-UMem-Token", "")
            if not supplied:
                supplied = (self._qs().get("t") or [""])[0]
            return bool(supplied) and secrets.compare_digest(supplied, token)

        @staticmethod
        def _err(e: Exception) -> int:
            if isinstance(e, NotFound):
                return 404
            if isinstance(e, Denied):
                return 403
            if isinstance(e, ApprovalRequired):
                return 409
            return 400

        def _api_get(self, path: str, qs: dict) -> None:
            def one(k: str, d: str = "") -> str:
                return (qs.get(k) or [d])[0]

            def num(k: str, d: int) -> int:
                try:
                    return int(one(k, str(d)))
                except ValueError:
                    return d

            if path == "/api/stats":
                self._json(200, {
                    "records": engine.store.count(),
                    "namespaces": [
                        {**n, "about": NS_INFO.get(n["ns"], "")}
                        for n in engine.store.namespaces()],
                    "actors": engine.store.actors(),
                    "pending": len(engine.store.approvals("pending")),
                    "agents": detect_agents(),
                    "recent": engine.store.list(None, 8),
                })
            elif path == "/api/namespaces":
                self._json(200, [
                    {**n, "about": NS_INFO.get(n["ns"], "")}
                    for n in engine.store.namespaces()])
            elif path == "/api/records":
                q = one("q")
                ns = one("ns") or None
                limit = min(num("limit", 250), 2000)
                rows = (engine.search(q, ns, limit) if q
                        else engine.list(ns, limit, one("tag") or None,
                                         one("actor") or None))
                self._json(200, {"count": len(rows), "results": rows,
                                 "actors": [a["actor"]
                                            for a in engine.store.actors()]})
            elif path == "/api/record":
                self._json(200, engine.get(one("ns") or None, one("key")))
            elif path == "/api/search":
                rows = engine.search(one("q"), one("ns") or None,
                                     min(num("limit", 50), 500))
                self._json(200, {"count": len(rows), "results": rows})
            elif path == "/api/agents":
                self._json(200, detect_agents())
            elif path == "/api/overlap":
                self._json(200, compute_overlap(engine.store))
            elif path == "/api/audit":
                self._json(200, engine.store.audit_tail(min(num("limit", 100), 1000)))
            elif path == "/api/approvals":
                now = now_iso()
                rows = engine.store.approvals("pending")
                for r in rows:
                    r["expired"] = r["expires_at"] <= now
                self._json(200, rows)
            else:
                self._json(404, {"error": f"unknown endpoint {path}"})

        def _api_post(self, path: str) -> None:
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "bad JSON"})
                return
            human = os.environ.get("USER", "unknown")
            if path == "/api/delete-request":
                res = engine.delete_request(body.get("ns"), body.get("key", ""))
                res.update({"deleted": False, "approval_required": True,
                            "note": "Nothing was deleted. A human must run: "
                                    + res["approve_with"]})
                self._json(200, res)
            elif path == "/api/approve":
                self._json(200, engine.store.approve(body.get("token", ""), human))
            elif path == "/api/deny":
                self._json(200, engine.store.deny(body.get("token", ""), human))
            elif path.startswith("/api/agents/") and path.endswith("/connect"):
                agent_id = path[len("/api/agents/"):-len("/connect")]
                self._json(200, connect_agent(agent_id))
            else:
                self._json(404, {"error": f"unknown endpoint {path}"})

        def do_OPTIONS(self):
            self._send(204, b"", "text/plain")

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/health":
                self._json(200, {
                    "ok": True, "server": SERVER_NAME, "version": VERSION,
                    "surfaces": surfaces,
                    "mcp_endpoint": "/mcp" if mcp else None,
                    "dashboard": "/" if dashboard else None,
                    "auth": "bearer"})
                return
            if path in ("/", "/index.html", "/app"):
                if not dashboard:
                    self._json(404, {"error": "no dashboard on this port — "
                                              "run `universal-memory web`"})
                    return
                self._send(200, WEB_HTML.encode(), "text/html; charset=utf-8")
                return
            if path.startswith("/api/"):
                if not dashboard:
                    self._json(404, {"error": "no API on this port — "
                                              "run `universal-memory web`"})
                    return
                if not self._authed():
                    self._json(401, {"error": "unauthorized"})
                    return
                try:
                    self._api_get(path, self._qs())
                except UmemError as e:
                    self._json(self._err(e), {"error": str(e)})
                except Exception as e:  # noqa: BLE001
                    self._json(500, {"error": f"{type(e).__name__}: {e}"})
                return
            self._json(404, {"error": "not found"})

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if path.rstrip("/") == "/mcp":
                if not mcp:
                    self._json(404, {"error": "no MCP on this port — "
                                              "run `universal-memory serve`"})
                    return
                self._mcp()
                return
            if path.startswith("/api/"):
                if not dashboard:
                    self._json(404, {"error": "no API on this port — "
                                              "run `universal-memory web`"})
                    return
                if not self._authed():
                    self._json(401, {"error": "unauthorized"})
                    return
                try:
                    self._api_post(path)
                except UmemError as e:
                    self._json(self._err(e), {"error": str(e)})
                except Exception as e:  # noqa: BLE001
                    self._json(500, {"error": f"{type(e).__name__}: {e}"})
                return
            self._json(404, {"error": "not found"})

        def _mcp(self) -> None:
            if not self._authed():
                self._json(401, {"error": "missing or invalid bearer token"})
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "parse error"}})
                return
            with protocol_lock:
                if isinstance(payload, dict) and payload.get("method") == "initialize":
                    state["protocolHint"] = (payload.get("params") or {}).get(
                        "protocolVersion", state["protocolHint"])
                reply = _dispatch(engine, payload, state["protocolHint"])
            if reply is None:
                self._send(202, b"", "text/plain")
                return
            accept = self.headers.get("Accept", "")
            if "text/event-stream" in accept and "application/json" not in accept:
                data = json.dumps(reply, ensure_ascii=False)
                self._send(200, (f"event: message\ndata: {data}\n\n").encode(),
                           "text/event-stream")
                return
            self._json(200, reply)

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    if mcp:
        print(f"Universal Memory MCP over HTTP:  http://{host}:{port}/mcp",
              flush=True)
    else:
        print(f"Universal Memory dashboard:  http://{host}:{port}/", flush=True)
    print(f"Authorization: Bearer {token}", flush=True)
    if dashboard:
        print(f"Dashboard:  http://{host}:{port}/?t={token}", flush=True)
    print(f"health:  http://{host}:{port}/health", flush=True)
    print("Ctrl-C to stop", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
        engine.close()
    return 0


# --------------------------------------------------------------------------
# argparse
# --------------------------------------------------------------------------

def split_target(target: str) -> tuple[str | None, str]:
    """Accept 'key' or 'namespace:key'."""
    if target is None:
        raise UmemError("missing record target (expected 'key' or 'ns:key')")
    if ":" in target:
        ns, _, key = target.partition(":")
        if not key:
            raise UmemError(f"missing key in {target!r}")
        return (ns or None), key
    return None, target


def apply_target(args) -> None:
    ns, key = split_target(getattr(args, "target", None))
    args.ns = ns
    args.key = key


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Universal Memory - shared memory for every agent on this "
                    "machine. read/write/append/search are free; delete needs a "
                    "human.")
    p.add_argument("--version", action="version",
                   version=f"{PROG} {VERSION}")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    def add(name, fn, help_, **kw):
        sp = sub.add_parser(name, help=help_, **kw)
        sp.set_defaults(fn=fn)
        return sp

    def tgt(sp, name="target"):
        sp.add_argument(name, help="record target: 'key' or 'namespace:key'")

    add("init", cmd_init, "create config + database, print integration snippets")

    sp = add("put", cmd_put, "create or overwrite a record")
    tgt(sp)
    sp.add_argument("value", nargs="?", help="value ('-' or --stdin reads stdin)")
    sp.add_argument("--stdin", action="store_true", help="read value from stdin")
    sp.add_argument("--tags", help="comma separated tags")
    sp.add_argument("--type", choices=["kv", "log"], default="kv")
    sp.add_argument("--json", action="store_true")

    sp = add("get", cmd_get, "read a record")
    tgt(sp)
    sp.add_argument("--json", action="store_true", help="include metadata")

    sp = add("append", cmd_append, "append text to a record")
    tgt(sp)
    sp.add_argument("value", nargs="?", help="text to append ('-' or --stdin)")
    sp.add_argument("--stdin", action="store_true")
    sp.add_argument("--json", action="store_true")

    sp = add("log", cmd_log, "append a timestamped line to a log record")
    tgt(sp)
    sp.add_argument("value", nargs="?", help="line to log")
    sp.add_argument("--stdin", action="store_true")
    sp.add_argument("--json", action="store_true")

    sp = add("del", cmd_del, "delete a record (requires human approval)")
    tgt(sp)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--yes", action="store_true",
                    help="human already consented on the command line")

    sp = add("approve", cmd_approve, "human: approve a pending delete token")
    sp.add_argument("token")
    sp.add_argument("--json", action="store_true")

    sp = add("deny", cmd_deny, "human: reject a pending delete token")
    sp.add_argument("token")
    sp.add_argument("--json", action="store_true")

    sp = add("pending", cmd_pending, "list pending delete approvals")
    sp.add_argument("--json", action="store_true")

    sp = add("search", cmd_search, "full-text search")
    sp.add_argument("query")
    sp.add_argument("--ns", default=None)
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--json", action="store_true")

    sp = add("list", cmd_list, "list records")
    sp.add_argument("--ns", default=None)
    sp.add_argument("--tag", default=None)
    sp.add_argument("--actor", default=None)
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--namespaces", action="store_true", help="list namespaces")
    sp.add_argument("--json", action="store_true")

    sp = add("whoami", cmd_whoami, "show actor identity, paths, namespaces")
    sp.add_argument("--json", action="store_true")

    sp = add("import", cmd_import, "bulk import a text file as records")
    sp.add_argument("file")
    sp.add_argument("--ns", default=None)
    sp.add_argument("--split-by", default=r"\n§\n|\n§|\n\n---\n",
                    help="regex used to split the file into records")
    sp.add_argument("--tags", default="")
    sp.add_argument("--json", action="store_true")

    sp = add("mcp", cmd_mcp, "run MCP server over stdio")
    sp = add("serve", cmd_serve,
             "run the MCP HTTP endpoint (continuous; browser agents use it)")
    sp.add_argument("--host", default=None)
    sp.add_argument("--port", type=int, default=None)
    sp.add_argument("--dashboard", action="store_true",
                    help="also mount the web dashboard on this port")

    sp = add("web", cmd_web,
             "run the web dashboard + JSON API (on demand, own port)")
    sp.add_argument("--host", default=None)
    sp.add_argument("--port", type=int, default=None)
    sp.add_argument("--mcp", action="store_true",
                    help="also mount the MCP endpoint on this port")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return 1

    # resolve 'key' / 'ns:key' targets into (ns, key) before dispatch
    if getattr(args, "target", None) is not None:
        apply_target(args)

    cfg = load_config()
    eng = None
    try:
        if args.cmd == "init":
            return cmd_init(args, cfg)
        eng = Engine(cfg)
        return args.fn(args, cfg, eng)
    except NotFound as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return e.exit_code
    except ApprovalRequired as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return e.exit_code
    except Denied as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return e.exit_code
    except UmemError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        return 130
    finally:
        if eng is not None:
            eng.close()


if __name__ == "__main__":
    sys.exit(main())

"""F4 — log of Hermes' calls to LLM providers (main turns, subagents, auxiliary calls).

Fed by Hermes' own observer hooks:
  pre_api_request / post_api_request   every provider request of an agent turn
                                       (subagents run the same loop, so they are included)
  api_request_error                    a provider call that raised
  pre_auxiliary_call / post_auxiliary_call   titling, compression, vision, approval …

Always stored (``llm.db``, separate from the ledger): metadata per call — provider, model,
session/turn ids, tokens, timing, finish reason, errors. Never the API key: Hermes itself
replaces api_key / authorization / cookie values with ``<redacted>`` before plugins see them.

Optional (setting ``llm_capture_content``, OFF by default): the sanitized request and response
JSON Hermes hands to plugins — capped by Hermes (8 000 chars per string, 50 000 per payload by
default) and additionally masked here for secret-looking strings. Kept only
``llm_content_retention_days`` (7) days. This is Hermes' view of the call, not the exact bytes
on the wire.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DEFAULT_RETENTION_DAYS = 90
DEFAULT_CONTENT_RETENTION_DAYS = 7
PENDING_MAX = 512
PENDING_TTL = 30 * 60

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,})"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    re.compile(r"\b(xox[abprs]-[A-Za-z0-9-]{10,})"),
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
    re.compile(r"\b(AIza[0-9A-Za-z_-]{35})\b"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(r"(https?://)[^/@\s\"']+@"),
]


def mask_secrets(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        if pat.pattern.startswith("(https?://)"):
            text = pat.sub(r"\1***@", text)
        elif pat.pattern.startswith("(?i)\\b(bearer"):
            text = pat.sub(r"\1***", text)
        else:
            text = pat.sub("***", text)
    return text


def safe_base_url(url: Any) -> str | None:
    """scheme://host/path only — a query string could carry a key."""
    if not url:
        return None
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return None
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    # ".../codex" and ".../codex/" are the same endpoint
    return urlunsplit((parts.scheme, host, parts.path.rstrip("/"), "", "")) or None


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  ended_at REAL,
  kind TEXT NOT NULL,              -- turn | aux
  aux_task TEXT,
  status TEXT NOT NULL,            -- ok | error
  profile TEXT,
  platform TEXT,
  session_id TEXT,
  task_id TEXT,
  turn_id TEXT,
  api_request_id TEXT,
  provider TEXT,
  upstream_provider TEXT,
  model TEXT,
  response_model TEXT,
  base_url TEXT,
  api_mode TEXT,
  api_call_count INTEGER,
  message_count INTEGER,
  tool_count INTEGER,
  approx_input_tokens INTEGER,
  max_tokens INTEGER,
  duration REAL,
  ttfb REAL,
  finish_reason TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_write_tokens INTEGER,
  reasoning_tokens INTEGER,
  assistant_content_chars INTEGER,
  assistant_tool_call_count INTEGER,
  error_type TEXT,
  error_message TEXT,
  status_code INTEGER,
  retry_count INTEGER,
  has_content INTEGER NOT NULL DEFAULT 0,
  context_length INTEGER,
  prompt_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS calls_session ON calls(session_id, ts);
CREATE INDEX IF NOT EXISTS calls_provider ON calls(provider, model, ts);
CREATE TABLE IF NOT EXISTS content (
  call_id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  request_json TEXT,
  response_json TEXT
);
CREATE INDEX IF NOT EXISTS content_ts ON content(ts);
"""

_COLUMNS = (
    "ts", "ended_at", "kind", "aux_task", "status", "profile", "platform", "session_id", "task_id", "turn_id",
    "api_request_id", "provider", "upstream_provider", "model", "response_model", "base_url", "api_mode",
    "api_call_count", "message_count", "tool_count", "approx_input_tokens", "max_tokens", "duration", "ttfb",
    "finish_reason", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
    "assistant_content_chars", "assistant_tool_call_count", "error_type", "error_message", "status_code",
    "retry_count", "has_content", "context_length", "prompt_tokens",
)
_ADDED_COLUMNS = {"context_length": "INTEGER", "prompt_tokens": "INTEGER"}  # v2 (F5: session capacity)


class LlmLog:
    def __init__(self, db_path: str | os.PathLike[str], *, retention_days: float = DEFAULT_RETENTION_DAYS,
                 content_retention_days: float = DEFAULT_CONTENT_RETENTION_DAYS):
        self.db_path = Path(db_path)
        self.retention_days = retention_days
        self.content_retention_days = content_retention_days
        self._lock = threading.Lock()
        self._ready = False
        self._last_prune = 0.0
        self._pending: dict[str, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()

    # ── storage ──────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=3.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=3000")
        if not self._ready:
            conn.execute("PRAGMA journal_mode=WAL")
            existing = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
            if existing:
                for col, typ in _ADDED_COLUMNS.items():
                    if col not in existing:
                        conn.execute(f"ALTER TABLE calls ADD COLUMN {col} {typ}")
            conn.executescript(_SCHEMA)
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', '2')")
            self._ready = True
        return conn

    def _insert(self, row: dict[str, Any], content: tuple[str | None, str | None] | None) -> int:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        row = {c: row.get(c) for c in _COLUMNS}
        row["has_content"] = int(bool(content))
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN")
                cur = conn.execute(
                    f"INSERT INTO calls({', '.join(_COLUMNS)}) VALUES({', '.join(':' + c for c in _COLUMNS)})", row
                )
                call_id = int(cur.lastrowid)
                if content:
                    conn.execute("INSERT INTO content(call_id, ts, request_json, response_json) VALUES(?, ?, ?, ?)",
                                 (call_id, row["ts"], content[0], content[1]))
                conn.execute("COMMIT")
                self._maybe_prune(conn)
                return call_id
            finally:
                conn.close()

    def _maybe_prune(self, conn: sqlite3.Connection) -> None:
        now = time.time()
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        if self.retention_days > 0:
            conn.execute("DELETE FROM calls WHERE ts < ?", (now - self.retention_days * 86400,))
        if self.content_retention_days > 0:
            old = now - self.content_retention_days * 86400
            conn.execute("DELETE FROM content WHERE ts < ?", (old,))
            conn.execute("UPDATE calls SET has_content = 0 WHERE has_content = 1 AND ts < ?", (old,))

    # ── hook entry points ────────────────────────────────────────────────────

    def _stash(self, key: str | None, data: dict[str, Any]) -> None:
        if not key:
            return
        now = time.time()
        with self._pending_lock:
            if len(self._pending) >= PENDING_MAX:
                for k in [k for k, v in self._pending.items() if now - v["_t"] > PENDING_TTL] or list(self._pending)[: PENDING_MAX // 4]:
                    self._pending.pop(k, None)
            self._pending[key] = {**data, "_t": now}

    def _take(self, key: str | None) -> dict[str, Any]:
        if not key:
            return {}
        with self._pending_lock:
            return self._pending.pop(key, {}) or {}

    def on_pre(self, payload: dict[str, Any], *, capture: bool) -> None:
        """pre_api_request / pre_auxiliary_call: remember what only the request side knows."""
        data = {
            "tool_count": _int(payload.get("tool_count")),
            "approx_input_tokens": _int(payload.get("approx_input_tokens")),
            "max_tokens": _int(payload.get("max_tokens")),
        }
        if capture and payload.get("request") is not None:
            data["request"] = payload.get("request")  # already sanitized and capped by Hermes
        self._stash(payload.get("api_request_id"), data)

    def on_post(self, payload: dict[str, Any], *, kind: str, profile: str | None, capture: bool) -> int:
        """post_api_request / post_auxiliary_call → one row (+ content when captured)."""
        pre = self._take(payload.get("api_request_id"))
        usage = payload.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        error = payload.get("error")
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        started = _float(payload.get("started_at")) or time.time()
        first_chunk = _float(payload.get("first_chunk_at"))
        row = {
            "ts": started,
            "ended_at": _float(payload.get("ended_at")),
            "kind": kind,
            "aux_task": payload.get("aux_task"),
            "status": "error" if error else "ok",
            "profile": profile,
            "platform": payload.get("platform") or None,
            "session_id": payload.get("session_id") or None,
            "task_id": payload.get("task_id") or None,
            "turn_id": payload.get("turn_id") or None,
            "api_request_id": payload.get("api_request_id") or None,
            "provider": payload.get("provider"),
            "upstream_provider": response.get("upstream_provider"),
            "model": payload.get("model"),
            "response_model": payload.get("response_model"),
            "base_url": safe_base_url(payload.get("base_url")),
            "api_mode": payload.get("api_mode"),
            "api_call_count": _int(payload.get("api_call_count")),
            "message_count": _int(payload.get("message_count")),
            "tool_count": pre.get("tool_count", _int(payload.get("tool_count"))),
            "approx_input_tokens": pre.get("approx_input_tokens", _int(payload.get("approx_input_tokens"))),
            "max_tokens": pre.get("max_tokens", _int(payload.get("max_tokens"))),
            "duration": _float(payload.get("api_duration")),
            "ttfb": (first_chunk - started) if first_chunk and started else None,
            "finish_reason": payload.get("finish_reason"),
            "input_tokens": _int(usage.get("input_tokens")),
            "output_tokens": _int(usage.get("output_tokens")),
            "cache_read_tokens": _int(usage.get("cache_read_tokens")),
            "cache_write_tokens": _int(usage.get("cache_write_tokens")),
            "reasoning_tokens": _int(usage.get("reasoning_tokens")),
            "assistant_content_chars": _int(payload.get("assistant_content_chars")),
            "assistant_tool_call_count": _int(payload.get("assistant_tool_call_count")),
            "error_type": payload.get("error_type"),
            "error_message": mask_secrets(str(error))[:500] if error else None,
            "retry_count": _int(payload.get("retry_count")),
            "context_length": _int(payload.get("context_length")),
        }
        if usage:
            row["prompt_tokens"] = (row["input_tokens"] or 0) + (row["cache_read_tokens"] or 0) + (row["cache_write_tokens"] or 0)
        content = None
        if capture:
            req = pre.get("request")
            content = (
                mask_secrets(json.dumps(req, ensure_ascii=False, default=str)) if req is not None else None,
                mask_secrets(json.dumps(response, ensure_ascii=False, default=str)) if response else None,
            )
            if content == (None, None):
                content = None
        return self._insert(row, content)

    def on_error(self, payload: dict[str, Any], *, profile: str | None, capture: bool) -> int:
        """api_request_error: a provider call that raised (one row per failed attempt)."""
        pre = self._take(payload.get("api_request_id"))
        err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        row = {
            "ts": _float(payload.get("started_at")) or time.time(),
            "ended_at": _float(payload.get("ended_at")),
            "kind": "turn",
            "status": "error",
            "profile": profile,
            "platform": payload.get("platform") or None,
            "session_id": payload.get("session_id") or None,
            "task_id": payload.get("task_id") or None,
            "turn_id": payload.get("turn_id") or None,
            "api_request_id": payload.get("api_request_id") or None,
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "base_url": safe_base_url(payload.get("base_url")),
            "api_mode": payload.get("api_mode"),
            "api_call_count": _int(payload.get("api_call_count")),
            "tool_count": pre.get("tool_count"),
            "approx_input_tokens": pre.get("approx_input_tokens"),
            "max_tokens": pre.get("max_tokens"),
            "duration": _float(payload.get("api_duration")),
            "error_type": err.get("type") or payload.get("reason"),
            "error_message": mask_secrets(str(err.get("message") or payload.get("reason") or ""))[:500] or None,
            "status_code": _int(payload.get("status_code")),
            "retry_count": _int(payload.get("retry_count")),
        }
        content = None
        if capture:
            req = pre.get("request") if pre.get("request") is not None else payload.get("request")
            if req is not None:
                content = (mask_secrets(json.dumps(req, ensure_ascii=False, default=str)), None)
        return self._insert(row, content)

    # ── reading ──────────────────────────────────────────────────────────────

    def _ro(self) -> sqlite3.Connection | None:
        if not self.db_path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        return conn

    def calls(self, *, since: float | None = None, session_id: str | None = None, limit: int = 50000) -> list[dict[str, Any]]:
        conn = self._ro()
        if conn is None:
            return []
        sql, params = "SELECT * FROM calls WHERE 1=1", []
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def session_meta(self, session_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Per session: context fill of the latest main-turn call and the providers/models used."""
        out: dict[str, dict[str, Any]] = {}
        ids = [s for s in session_ids if s][:500]
        conn = self._ro()
        if conn is None or not ids:
            return out
        marks = ",".join("?" * len(ids))
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
            if "context_length" in cols:
                for r in conn.execute(
                    f"SELECT session_id, prompt_tokens, context_length, ts FROM calls WHERE session_id IN ({marks}) "
                    "AND kind = 'turn' AND status = 'ok' AND prompt_tokens IS NOT NULL ORDER BY ts", ids
                ):
                    out.setdefault(r[0], {})["context"] = {"used": r[1], "max": r[2], "ts": r[3]}
            for r in conn.execute(
                f"SELECT session_id, provider, model, COUNT(*), MAX(ts), SUM(status = 'error') FROM calls WHERE session_id IN ({marks}) "
                "GROUP BY session_id, provider, model", ids
            ):
                out.setdefault(r[0], {}).setdefault("providers", []).append(
                    {"provider": r[1], "model": r[2], "calls": r[3], "last_ts": r[4], "errors": r[5] or 0, "source": "llm_log"})
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()
        return out

    def content(self, call_id: int) -> dict[str, Any] | None:
        conn = self._ro()
        if conn is None:
            return None
        try:
            call = conn.execute("SELECT * FROM calls WHERE id = ?", (int(call_id),)).fetchone()
            if call is None:
                return None
            body = conn.execute("SELECT request_json, response_json FROM content WHERE call_id = ?", (int(call_id),)).fetchone()
            return {"call": dict(call), "request": body[0] if body else None, "response": body[1] if body else None}
        except sqlite3.OperationalError:
            return None
        finally:
            conn.close()


# ── summary ──────────────────────────────────────────────────────────────────


def _add(acc: dict[str, Any], c: dict[str, Any]) -> None:
    acc["calls"] += 1
    acc["errors"] += 1 if c["status"] == "error" else 0
    for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"):
        acc[k] += c.get(k) or 0
    if c.get("duration") is not None and c["status"] == "ok":
        acc["_dur"].append(c["duration"])
    acc["last_ts"] = max(acc["last_ts"], c["ts"])


def _new() -> dict[str, Any]:
    return {"calls": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "reasoning_tokens": 0, "_dur": [], "last_ts": 0.0}


def _finish(acc: dict[str, Any]) -> dict[str, Any]:
    d = sorted(acc.pop("_dur"))
    acc["avg_duration"] = round(sum(d) / len(d), 2) if d else None
    acc["p95_duration"] = round(d[min(len(d) - 1, int(len(d) * 0.95))], 2) if d else None
    return acc


def summarize_calls(calls: list[dict[str, Any]], *, max_errors: int = 15, max_sessions: int = 10) -> dict[str, Any]:
    total = _new()
    providers: dict[str, dict[str, Any]] = {}
    sessions: dict[str, dict[str, Any]] = {}
    errors = []
    for c in calls:  # newest first
        _add(total, c)
        prov = c.get("provider") or "?"
        p = providers.setdefault(prov, {**_new(), "provider": prov, "models": {}, "aux": {}, "hosts": set()})
        _add(p, c)
        if c.get("base_url"):
            p["hosts"].add(c["base_url"].rstrip("/"))  # rows stored before the trailing-slash fix
        m = p["models"].setdefault(c.get("model") or "?", {**_new(), "model": c.get("model") or "?"})
        _add(m, c)
        if c.get("kind") == "aux":
            a = p["aux"].setdefault(c.get("aux_task") or "?", {**_new(), "aux_task": c.get("aux_task") or "?"})
            _add(a, c)
        if c.get("session_id"):
            s = sessions.setdefault(c["session_id"], {**_new(), "session_id": c["session_id"], "models": set()})
            _add(s, c)
            s["models"].add(f"{prov}/{c.get('model') or '?'}")
        if c["status"] == "error" and len(errors) < max_errors:
            errors.append({k: c.get(k) for k in ("id", "ts", "provider", "model", "kind", "aux_task", "status_code", "error_type", "error_message", "session_id")})
    out_providers = []
    for p in providers.values():
        p["models"] = sorted((_finish(m) for m in p["models"].values()), key=lambda m: -m["calls"])
        p["aux"] = sorted((_finish(a) for a in p["aux"].values()), key=lambda a: -a["calls"])
        p["hosts"] = sorted(p["hosts"])
        out_providers.append(_finish(p))
    out_providers.sort(key=lambda p: -p["calls"])
    top_sessions = sorted(sessions.values(), key=lambda s: -(s["input_tokens"] + s["output_tokens"] + s["cache_read_tokens"]))[:max_sessions]
    for s in top_sessions:
        s["models"] = sorted(s["models"])
        _finish(s)
    return {"total": _finish(total), "providers": out_providers, "sessions": top_sessions, "errors": errors}


def default_db_path(ledger_path: Path) -> Path:
    return ledger_path.parent / "llm.db"

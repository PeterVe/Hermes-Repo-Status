"""F2b — rebuild the ledger from Hermes session history (``state.db``), read-only.

``state.db`` is Hermes' INTERNAL storage, not a plugin API: its schema can change
with any update. This module therefore
  * opens every database read-only (``mode=ro`` + ``PRAGMA query_only``),
  * checks the exact columns it needs before reading, and refuses with a clear
    message on a mismatch (the rest of the plugin keeps working),
  * marks every imported row ``source='history'``,
  * skips tool calls the ledger already has (same ``tool_call_id``), so it can
    be run again safely and never duplicates what the live hook recorded.

Self-contained (no Hermes imports beyond the optional root lookup in ledger.py).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterator

try:  # package import (agent half) or flat import (tests)
    from . import ledger as _ledger  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - flat layout
    import ledger as _ledger  # type: ignore[no-redef]

TESTED_SCHEMA_VERSIONS = {32}
REQUIRED_COLUMNS = {
    "messages": {"id", "session_id", "role", "content", "tool_call_id", "tool_calls", "tool_name", "timestamp"},
    "sessions": {"id", "cwd"},
}
OPTIONAL_SESSION_COLUMNS = ("profile_name", "parent_session_id")
TRACKED = ("write_file", "patch", "terminal", "execute_code", "skill_manage", "memory", "cronjob_manage", "cronjob")
# whose results decide success / real paths
RESULT_TOOLS = ("write_file", "patch", "skill_manage", "memory", "cronjob_manage", "cronjob")
BATCH = 2000


class HistorySchemaError(Exception):
    """state.db does not look like the schema this importer understands."""


def find_state_dbs(root: Path | None = None) -> list[tuple[str, Path]]:
    """(profile, state.db) for the default home and every named profile."""
    root = Path(root) if root else _ledger.hermes_root()
    out: list[tuple[str, Path]] = []
    if (root / "state.db").exists():
        out.append(("default", root / "state.db"))
    profiles = root / "profiles"
    if profiles.is_dir():
        for child in sorted(profiles.iterdir()):
            db = child / "state.db"
            if child.is_dir() and db.exists():
                out.append((child.name, db))
    return out


def _open_ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5.0)
    conn.execute("PRAGMA query_only=1")
    return conn


def check_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    info: dict[str, Any] = {"version": None, "tested": False, "session_columns": set()}
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        info["version"] = row[0] if row else None
    except sqlite3.Error:
        pass
    info["tested"] = info["version"] in TESTED_SCHEMA_VERSIONS
    for table, needed in REQUIRED_COLUMNS.items():
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        missing = needed - cols
        if not cols:
            raise HistorySchemaError(f"tabuľka {table} v state.db chýba")
        if missing:
            raise HistorySchemaError(f"state.db.{table} nemá stĺpce {sorted(missing)} (schéma {info['version']})")
        if table == "sessions":
            info["session_columns"] = cols
    return info


def _sessions(conn: sqlite3.Connection, cols: set[str]) -> dict[str, dict[str, Any]]:
    extra = [c for c in OPTIONAL_SESSION_COLUMNS if c in cols]
    sql = "SELECT id, cwd" + "".join(f", {c}" for c in extra) + " FROM sessions"
    out = {}
    for row in conn.execute(sql):
        rec = {"cwd": row[1]}
        for i, c in enumerate(extra, start=2):
            rec[c] = row[i]
        out[row[0]] = rec
    return out


def _results(conn: sqlite3.Connection, since: float) -> dict[str, str]:
    marks = ",".join("?" * len(RESULT_TOOLS))
    sql = f"SELECT tool_call_id, content FROM messages WHERE role='tool' AND tool_name IN ({marks}) AND timestamp >= ? AND tool_call_id IS NOT NULL"
    return {cid: content for cid, content in conn.execute(sql, (*RESULT_TOOLS, since))}


def _calls(conn: sqlite3.Connection, since: float) -> Iterator[tuple[str, float, dict[str, Any]]]:
    like = " OR ".join("tool_calls LIKE ?" for _ in TRACKED)
    sql = (
        "SELECT session_id, timestamp, tool_calls FROM messages "
        f"WHERE role='assistant' AND tool_calls IS NOT NULL AND timestamp >= ? AND ({like}) ORDER BY id"
    )
    cur = conn.execute(sql, (since, *[f'%"{t}"%' for t in TRACKED]))
    while True:
        rows = cur.fetchmany(BATCH)
        if not rows:
            return
        for session_id, ts, raw in rows:
            try:
                calls = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(calls, list):
                continue
            for call in calls:
                if isinstance(call, dict):
                    yield session_id, float(ts or 0), call


def _parse_call(call: dict[str, Any]) -> tuple[str | None, str | None, dict[str, Any]]:
    fn = call.get("function") or {}
    name = fn.get("name") or call.get("name")
    cid = call.get("id") or call.get("call_id")
    raw = fn.get("arguments", call.get("arguments"))
    if isinstance(raw, dict):
        args = raw
    else:
        try:
            args = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            args = {}
    return name, cid, args if isinstance(args, dict) else {}


def import_history(
    target: "_ledger.Ledger",
    *,
    days: float = 90,
    dry_run: bool = False,
    root: Path | None = None,
    dbs: list[tuple[str, Path]] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Import tool calls from the last ``days`` of history into ``target``. Returns a report."""
    since = time.time() - days * 86400 if days and days > 0 else 0.0
    known = target.known_tool_call_ids()
    report: dict[str, Any] = {"since": since, "dry_run": dry_run, "profiles": []}
    for profile, db in dbs if dbs is not None else find_state_dbs(root):
        entry: dict[str, Any] = {"profile": profile, "db": str(db), "calls": 0, "skipped_known": 0, "failed": 0, "rows": 0, "by_tool": {}}
        report["profiles"].append(entry)
        try:
            conn = _open_ro(db)
        except sqlite3.Error as exc:
            entry["error"] = f"state.db sa nedá otvoriť len na čítanie: {exc}"
            continue
        try:
            info = check_schema(conn)
            entry["schema_version"] = info["version"]
            entry["schema_tested"] = info["tested"]
            sessions = _sessions(conn, info["session_columns"])
            results = _results(conn, since)
            pending: list[dict[str, Any]] = []
            for session_id, ts, call in _calls(conn, since):
                name, cid, args = _parse_call(call)
                if name not in TRACKED:
                    continue
                entry["calls"] += 1
                if cid and cid in known:
                    entry["skipped_known"] += 1
                    continue
                result = results.get(cid) if cid else None
                if name in RESULT_TOOLS and result is None:
                    entry["failed"] += 1  # no result recorded: the call never completed
                    continue
                sess = sessions.get(session_id) or {}
                rows = _ledger.build_rows(
                    name,
                    args,
                    result or "{}",
                    cwd=sess.get("cwd") or None,
                    profile=sess.get("profile_name") or profile,
                    session_id=session_id,
                    task_id=None,
                    now=ts,
                    tool_call_id=cid,
                    source="history",
                )
                if not rows and name in RESULT_TOOLS:
                    entry["failed"] += 1
                entry["by_tool"][name] = entry["by_tool"].get(name, 0) + len(rows)
                pending.extend(rows)
                if cid:
                    known.add(cid)
                if len(pending) >= BATCH:
                    entry["rows"] += len(pending) if dry_run else target.record(pending)
                    pending = []
                    if progress:
                        progress(f"{profile}: {entry['rows']} záznamov…")
            if pending:
                entry["rows"] += len(pending) if dry_run else target.record(pending)
        except HistorySchemaError as exc:
            entry["error"] = str(exc)
        except sqlite3.Error as exc:
            entry["error"] = f"čítanie state.db zlyhalo: {exc}"
        finally:
            conn.close()
    report["total_rows"] = sum(p["rows"] for p in report["profiles"])
    return report


def render_report(report: dict[str, Any]) -> str:
    lines = ["Import histórie do ledgeru" + (" (skúška, nič sa nezapísalo)" if report.get("dry_run") else "")]
    if report.get("since"):
        lines.append("Od: " + time.strftime("%Y-%m-%d %H:%M", time.localtime(report["since"])))
    for p in report["profiles"]:
        lines.append(f"\nProfil {p['profile']} – {p['db']}")
        if p.get("schema_version") is not None and not p.get("schema_tested"):
            lines.append(f"  ⚠️ schéma state.db verzia {p['schema_version']} nebola s pluginom testovaná (testované: {sorted(TESTED_SCHEMA_VERSIONS)})")
        if p.get("error"):
            lines.append(f"  ❌ {p['error']}")
            continue
        lines.append(f"  volaní nástrojov: {p['calls']}, už v ledgeri alebo duplicitných v histórii: {p['skipped_known']}, neúspešných/bez výsledku: {p['failed']}")
        lines.append(f"  nových záznamov: {p['rows']}  " + ", ".join(f"{k}: {v}" for k, v in sorted(p["by_tool"].items())))
    lines.append(f"\nSpolu nových záznamov: {report['total_rows']}")
    return "\n".join(lines)

"""Ledger of where Hermes wrote — phase F2 of repo-status.

A ``post_tool_call`` hook turns each file-writing tool call into rows of a small
SQLite database shared by every profile (``<hermes root>/plugin-shared/repo-status/
ledger.db``). Only paths and short command previews are stored, never file content.

Evidence levels (the plan's rule: never attribute a change without proof):
  documented  a file tool named the path (write_file, patch, MCP file tools)
  probable    a command ran in a folder (terminal, execute_code) — files unknown
  observed    added at query time: git shows the file is still uncommitted

Self-contained like gitprobe.py (loaded by file path from dashboard/plugin_api.py).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA_VERSION = 3  # v2: tool_call_id (dedup) · v3: category + item (Hermes settings, F3)
DEFAULT_RETENTION_DAYS = 90
COMMAND_PREVIEW = 200

DOCUMENTED = "documented"
PROBABLE = "probable"
OBSERVED = "observed"

_V4A_FILE = re.compile(r"^\*\*\* (Update|Add|Delete) File: (.+?)\s*$", re.MULTILINE)
_V4A_MOVE = re.compile(r"^\*\*\* Move to: (.+?)\s*$", re.MULTILINE)
_SECRETISH = re.compile(
    r"(?i)((?:api[_-]?key|token|secret|password|passwd|pwd|auth|bearer|credential)[\w-]*\s*[=:]\s*|--(?:password|token|api-key)[ =])(\"[^\"]*\"|'[^']*'|\S+)"
)
_URL_CRED = re.compile(r"(https?://)[^/@\s]+@")
_MCP_WRITE_VERBS = ("write", "edit", "create", "move", "rename", "delete", "remove", "patch", "save", "append")
_PATH_KEYS = ("path", "file_path", "filepath", "file", "filename", "destination", "dest", "target", "source", "new_path")


# ── extraction (pure) ────────────────────────────────────────────────────────


def redact_command(command: str) -> str:
    text = _URL_CRED.sub(r"\1***@", command or "")
    text = _SECRETISH.sub(lambda m: m.group(1) + "***", text)
    text = " ".join(text.split())
    return text[:COMMAND_PREVIEW] + ("…" if len(text) > COMMAND_PREVIEW else "")


def _loads(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    try:
        value = json.loads(result) if isinstance(result, str) else None
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _failed(res: dict[str, Any]) -> bool:
    if res.get("error"):
        return True
    return res.get("success") is False


def extract_events(tool_name: str, args: dict[str, Any] | None, result: Any) -> list[dict[str, Any]]:
    """Turn one tool call into ledger events (paths may still be relative)."""
    args = args or {}
    res = _loads(result)

    if tool_name == "write_file":
        if _failed(res) or not (args.get("path") or res.get("resolved_path")):
            return []
        # The tool reports the absolute path it actually wrote: no guessing needed.
        return [{"action": "write", "path": str(res.get("resolved_path") or args["path"]), "evidence": DOCUMENTED}]

    if tool_name == "patch":
        if _failed(res) or res.get("no_change"):
            return []
        events: list[dict[str, Any]] = []
        for key, action in (("files_modified", "patch"), ("files_created", "create"), ("files_deleted", "delete")):
            for p in res.get(key) or []:
                events.append({"action": action, "path": str(p), "evidence": DOCUMENTED})
        if events:
            return events
        if res.get("resolved_path"):
            return [{"action": "patch", "path": str(res["resolved_path"]), "evidence": DOCUMENTED}]
        if args.get("patch"):
            kinds = {"Update": "patch", "Add": "create", "Delete": "delete"}
            for kind, p in _V4A_FILE.findall(str(args["patch"])):
                events.append({"action": kinds[kind], "path": p, "evidence": DOCUMENTED})
            for p in _V4A_MOVE.findall(str(args["patch"])):
                events.append({"action": "create", "path": p, "evidence": DOCUMENTED})
            return events
        if args.get("path"):
            return [{"action": "patch", "path": str(args["path"]), "evidence": DOCUMENTED}]
        return []

    if tool_name == "terminal":
        command = str(args.get("command") or "").strip()
        if not command:
            return []
        return [{"action": "command", "path": args.get("workdir") or None, "evidence": PROBABLE, "detail": redact_command(command)}]

    if tool_name == "execute_code":
        code = str(args.get("code") or args.get("source") or "").strip()
        first = code.splitlines()[0] if code else "python"
        return [{"action": "code", "path": None, "evidence": PROBABLE, "detail": redact_command(first)}]

    if tool_name in SETTINGS_TOOLS or tool_name.startswith("cronjob"):
        return settings_tool_events(tool_name, args, res)

    lowered = tool_name.lower()
    if lowered.startswith("mcp") and any(v in lowered for v in _MCP_WRITE_VERBS):
        if _failed(res):
            return []
        events = []
        for key in _PATH_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                events.append({"action": "write", "path": value.strip(), "evidence": DOCUMENTED})
        return events

    return []


# ── F3: Hermes settings (skills, memory, cron) ───────────────────────────────

SETTINGS_TOOLS = {"skill_manage", "memory", "cronjob_manage", "cronjob"}
SETTINGS_CATEGORIES = ("skill", "memory", "cron", "config", "agent", "plugin", "hook")
_READ_ONLY_ACTIONS = {"list", "get", "view", "show", "read", "status", "search", "history", "describe", "runs", "inspect"}


def _ops(args: dict[str, Any]) -> list[dict[str, Any]]:
    ops = args.get("operations")
    if isinstance(ops, list) and ops:
        return [o for o in ops if isinstance(o, dict)]
    return [args]


def settings_tool_events(tool_name: str, args: dict[str, Any], res: dict[str, Any]) -> list[dict[str, Any]]:
    """skill_manage / memory / cronjob_*: what changed, never the content."""
    if _failed(res):
        return []
    events = []
    if tool_name == "skill_manage":
        for op in _ops(args):
            action = str(op.get("action") or "edit")
            if action in _READ_ONLY_ACTIONS:
                continue
            name = str(op.get("name") or args.get("name") or "?")
            file_path = op.get("file_path") or op.get("path")
            events.append({"action": action, "path": None, "evidence": DOCUMENTED, "category": "skill", "name": name,
                           "detail": f"{name}/{file_path}" if file_path else name})
    elif tool_name == "memory":
        target = str(args.get("target") or "memory")
        for op in _ops(args):
            action = str(op.get("action") or "edit")
            if action in _READ_ONLY_ACTIONS:
                continue
            events.append({"action": action, "path": None, "evidence": DOCUMENTED, "category": "memory", "name": target,
                           "detail": f"{target}: {action}"})
    else:  # cronjob_manage / cronjob
        action = str(args.get("action") or "edit")
        if action in _READ_ONLY_ACTIONS:
            return []
        name = str(args.get("name") or args.get("job_id") or "?")
        schedule = args.get("schedule")
        events.append({"action": action, "path": None, "evidence": DOCUMENTED, "category": "cron", "name": name,
                       "detail": f"{name}" + (f" ({schedule})" if schedule else "")})
    return events


def settings_category(path: str | None, homes: list[Path] | None = None) -> tuple[str, str] | None:
    """(category, name) when ``path`` is a Hermes setting inside one of the ``homes``."""
    if not path:
        return None
    homes = homes if homes is not None else hermes_homes()
    norm = os.path.normpath(path)
    p = os.path.normcase(norm)
    for home in homes:
        h = os.path.normcase(os.path.normpath(str(home)))
        if not p.startswith(h + os.sep):
            continue
        rel_orig = norm[len(h) + 1:].replace("\\", "/")  # keeps the file's own casing
        rel = rel_orig.lower()
        parts, orig_parts = rel.split("/"), rel_orig.split("/")
        if parts[0] == "profiles":
            return None  # handled by the profile's own home entry
        if rel == "config.yaml":
            return "config", "config.yaml"
        if rel == "soul.md":
            return "agent", "SOUL.md"
        if parts[0] == "memories" and rel.endswith(".md"):
            return "memory", orig_parts[-1]
        if parts[0] == "skills" and len(parts) > 1:
            return "skill", skill_name_for(Path(path))
        if rel == "cron/jobs.json":
            return "cron", "jobs.json"
        if parts[0] == "plugins" and len(parts) > 1:
            return "plugin", orig_parts[1]
        if parts[0] == "hooks" and len(parts) > 1:
            return "hook", orig_parts[1]
    return None


SKILLS_CATALOG_ITEM = "(skills-catalog)"


def skill_name_for(path: Path) -> str:
    """Skill = nearest folder holding SKILL.md (else the first folder under skills/).
    A file directly in skills/ (e.g. Hermes' .bundled_manifest) belongs to the catalog itself."""
    if path.parent.name.lower() == "skills" and not path.is_dir():
        return SKILLS_CATALOG_ITEM
    cur = path if path.is_dir() else path.parent
    for cand in (cur, *cur.parents):
        if cand.name.lower() == "skills":
            break
        try:
            if (cand / "SKILL.md").exists():
                return cand.name
        except OSError:
            break
    parts = path.as_posix().split("/skills/", 1)
    return parts[1].split("/")[0] if len(parts) == 2 else path.name


def hermes_homes(root: Path | None = None) -> list[Path]:
    root = Path(root) if root else hermes_root()
    homes = [root]
    profiles = root / "profiles"
    if profiles.is_dir():
        homes += [c for c in sorted(profiles.iterdir()) if c.is_dir()]
    return homes


# ── git root lookup (filesystem only — the hook must stay fast) ──────────────

_root_cache: dict[str, str | None] = {}
_root_lock = threading.Lock()


def git_root_of(path: str | os.PathLike[str]) -> str | None:
    """Nearest ancestor holding ``.git`` (dir or worktree file). Cached per directory."""
    p = Path(path)
    start = p if p.is_dir() else p.parent
    key = str(start).lower()
    with _root_lock:
        if key in _root_cache:
            return _root_cache[key]
    found = None
    for cand in (start, *start.parents):
        try:
            if (cand / ".git").exists():
                found = str(cand)
                break
        except OSError:
            break
    with _root_lock:
        if len(_root_cache) > 4096:
            _root_cache.clear()
        _root_cache[key] = found
    return found


def normalize(path: str, cwd: str | None) -> str | None:
    if not path:
        return cwd
    p = Path(os.path.expandvars(os.path.expanduser(path.strip().strip('"'))))
    if not p.is_absolute():
        if not cwd:
            return None
        p = Path(cwd) / p
    try:
        return os.path.normpath(str(p.absolute()))
    except OSError:
        return os.path.normpath(str(p))


# ── where the shared ledger lives / which profile is calling ─────────────────


def hermes_root() -> Path:
    """The default Hermes home (parent of ``profiles/``), shared by every profile."""
    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root())
    except Exception:
        home = os.environ.get("HERMES_HOME") or os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "hermes")
        p = Path(home)
        return p.parent.parent if p.parent.name == "profiles" else p


def default_ledger_path() -> Path:
    return hermes_root() / "plugin-shared" / "repo-status" / "ledger.db"


def current_profile(fallback: str | None = None) -> str:
    """Profile owning the current call (scope-aware: one gateway may serve several)."""
    try:
        from hermes_constants import get_hermes_home, profile_name_for_home

        name = profile_name_for_home(get_hermes_home())
        if name:
            return str(name)
        return "default"
    except Exception:
        return fallback or "default"


# ── storage ──────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  profile TEXT,
  session_id TEXT,
  task_id TEXT,
  tool TEXT NOT NULL,
  action TEXT NOT NULL,
  evidence TEXT NOT NULL,
  path TEXT,
  git_root TEXT,
  rel_path TEXT,
  cwd TEXT,
  path_guessed INTEGER NOT NULL DEFAULT 0,
  detail TEXT,
  source TEXT NOT NULL DEFAULT 'hook',
  tool_call_id TEXT,
  category TEXT,
  item TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, ts);
CREATE INDEX IF NOT EXISTS events_root ON events(git_root, ts);
CREATE TABLE IF NOT EXISTS settings_files (
  home TEXT NOT NULL,
  rel_path TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  category TEXT NOT NULL,
  item TEXT,
  PRIMARY KEY (home, rel_path)
);
"""

# column → DDL, added in place when an older ledger lacks it
_ADDED_COLUMNS = {
    "tool_call_id": "ALTER TABLE events ADD COLUMN tool_call_id TEXT",
    "category": "ALTER TABLE events ADD COLUMN category TEXT",
    "item": "ALTER TABLE events ADD COLUMN item TEXT",
}


class Ledger:
    def __init__(self, db_path: str | os.PathLike[str], *, retention_days: float = DEFAULT_RETENTION_DAYS):
        self.db_path = Path(db_path)
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._ready = False
        self._last_prune = 0.0

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=3.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=3000")
        return conn

    def _ensure(self, conn: sqlite3.Connection) -> None:
        if self._ready:
            return
        conn.execute("PRAGMA journal_mode=WAL")
        existed = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone() is not None
        if existed:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
            for col, sql in _ADDED_COLUMNS.items():
                if col not in cols:
                    conn.execute(sql)
        conn.executescript(_SCHEMA)
        conn.execute("CREATE INDEX IF NOT EXISTS events_call ON events(tool_call_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS events_category ON events(category, ts)")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        self._ready = True

    def known_tool_call_ids(self) -> set[str]:
        if not self.db_path.exists():
            return set()
        conn = self._connect()
        try:
            self._ensure(conn)
            return {r[0] for r in conn.execute("SELECT DISTINCT tool_call_id FROM events WHERE tool_call_id IS NOT NULL")}
        finally:
            conn.close()

    def record(self, rows: Iterable[dict[str, Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = self._connect()
            try:
                self._ensure(conn)
                conn.execute("BEGIN")
                conn.executemany(
                    "INSERT INTO events(ts, profile, session_id, task_id, tool, action, evidence, path, git_root, rel_path, cwd, path_guessed, detail, source, tool_call_id, category, item)"
                    " VALUES(:ts, :profile, :session_id, :task_id, :tool, :action, :evidence, :path, :git_root, :rel_path, :cwd, :path_guessed, :detail, :source, :tool_call_id, :category, :item)",
                    [{**_ROW_DEFAULTS, **r} for r in rows],
                )
                conn.execute("COMMIT")
                self._maybe_prune(conn)
            finally:
                conn.close()
        return len(rows)

    def _maybe_prune(self, conn: sqlite3.Connection) -> None:
        now = time.time()
        if self.retention_days <= 0 or now - self._last_prune < 3600:
            return
        self._last_prune = now
        conn.execute("DELETE FROM events WHERE ts < ?", (now - self.retention_days * 86400,))

    def query(
        self,
        *,
        session_id: str | None = None,
        since: float | None = None,
        profile: str | None = None,
        limit: int = 5000,
        settings_only: bool = False,
    ) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        sql = "SELECT * FROM events WHERE 1=1"
        params: list[Any] = []
        if settings_only:
            sql += " AND category IS NOT NULL"
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        if profile:
            sql += " AND profile = ?"
            params.append(profile)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()


_ROW_DEFAULTS = {
    "profile": None,
    "session_id": None,
    "task_id": None,
    "path": None,
    "git_root": None,
    "rel_path": None,
    "cwd": None,
    "path_guessed": 0,
    "detail": None,
    "source": "hook",
    "tool_call_id": None,
    "category": None,
    "item": None,
}


def build_rows(
    tool_name: str,
    args: dict[str, Any] | None,
    result: Any,
    *,
    cwd: str | None,
    profile: str | None,
    session_id: str | None,
    task_id: str | None,
    now: float | None = None,
    tool_call_id: str | None = None,
    source: str = "hook",
) -> list[dict[str, Any]]:
    """Events for one tool call, with absolute paths and their git roots."""
    rows = []
    ts = now or time.time()
    for ev in extract_events(tool_name, args, result):
        raw = ev.get("path")
        guessed = bool(raw) and not Path(os.path.expanduser(str(raw))).is_absolute()
        if raw is None and ev["evidence"] == PROBABLE:
            guessed = True  # the command's folder is the session cwd, as far as we know
        if raw:
            path = normalize(str(raw), cwd)
        else:
            path = None if ev.get("category") else cwd  # settings tool calls have no folder
        root = git_root_of(path) if path else None
        rel = None
        if path and root:
            try:
                rel = os.path.relpath(path, root).replace("\\", "/")
            except ValueError:
                rel = None
            if rel == ".":
                rel = None
        rows.append(
            {
                "ts": ts,
                "profile": profile,
                "session_id": session_id or None,
                "task_id": task_id or None,
                "tool": tool_name,
                "action": ev["action"],
                "evidence": ev["evidence"],
                "path": path,
                "git_root": root,
                "rel_path": rel,
                "cwd": cwd,
                "path_guessed": int(guessed),
                "detail": ev.get("detail"),
                "source": source,
                "tool_call_id": tool_call_id or None,
                "category": ev.get("category"),
                "item": ev.get("name"),
            }
        )
        if ev.get("category") is None and ev["evidence"] == DOCUMENTED and path:
            hit = settings_category(path)
            if hit:  # e.g. write_file into HERMES_HOME/skills/… is also a settings change
                rows[-1]["category"], rows[-1]["item"] = hit
    return rows


# ── grouping for display ─────────────────────────────────────────────────────


_drive_cache: dict[str, str | None] = {}


def _mapped_drive_target(drive: str) -> str | None:
    """UNC target of a mapped network drive (``R:`` → ``\\\\server\\share``), else None."""
    drive = drive.upper()
    if drive in _drive_cache:
        return _drive_cache[drive]
    target = None
    if os.name == "nt":
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if ctypes.windll.mpr.WNetGetConnectionW(drive, buf, ctypes.byref(size)) == 0:
                target = buf.value.rstrip("\\") or None
        except Exception:
            target = None
    _drive_cache[drive] = target
    return target


def _canon(p: str) -> str:
    """Comparable form of a path: a mapped drive (``R:\\x``) and its UNC target
    (``\\\\server\\share\\x``) compare equal. No realpath: server-side links make it unstable."""
    p = os.path.normpath(p)
    drive, rest = os.path.splitdrive(p)
    if len(drive) == 2 and drive[1] == ":":
        target = _mapped_drive_target(drive)
        if target:
            p = target + rest
    return os.path.normcase(p)


def _inside(path: str | None, base: str | None) -> bool:
    if not path or not base:
        return False
    a, b = _canon(path), _canon(base)
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:
        return False


def summarize_events(
    events: list[dict[str, Any]],
    *,
    cwd: str | None = None,
    status_for_root: Callable[[str], dict[str, Any] | None] | None = None,
    max_files: int = 50,
) -> dict[str, Any]:
    """Group events by git root; mark roots outside ``cwd``; add 'observed' git state."""
    groups: dict[str, dict[str, Any]] = {}
    for ev in events:  # newest first
        if ev.get("category") and not ev.get("path"):
            continue  # skill_manage / memory / cron calls belong to the settings view
        key = ev.get("git_root") or "__none__"
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "root": ev.get("git_root"),
                "in_repo": bool(ev.get("git_root")),
                "outside_cwd": False,
                "last_ts": ev["ts"],
                "sessions": set(),
                "profiles": set(),
                "files": {},
                "commands": [],
                "documented": 0,
                "probable": 0,
            }
        g["sessions"].add(ev.get("session_id"))
        g["profiles"].add(ev.get("profile"))
        g[ev["evidence"]] = g.get(ev["evidence"], 0) + 1
        if ev["evidence"] == DOCUMENTED and ev.get("path"):
            f = g["files"].get(ev["path"])
            if f is None:
                f = g["files"][ev["path"]] = {
                    "path": ev["path"],
                    "rel_path": ev.get("rel_path"),
                    "last_ts": ev["ts"],
                    "actions": [],
                    "tools": [],
                    "path_guessed": bool(ev.get("path_guessed")),
                    "evidence": DOCUMENTED,
                }
            if ev["action"] not in f["actions"]:
                f["actions"].append(ev["action"])
            if ev["tool"] not in f["tools"]:
                f["tools"].append(ev["tool"])
        elif ev["evidence"] == PROBABLE and len(g["commands"]) < 10:
            g["commands"].append({"ts": ev["ts"], "tool": ev["tool"], "detail": ev.get("detail"), "folder": ev.get("path")})

    out = []
    for g in groups.values():
        anchor = g["root"] or next(iter(g["files"]), None) or (g["commands"][0]["folder"] if g["commands"] else None)
        g["outside_cwd"] = bool(cwd) and not (_inside(anchor, cwd) or _inside(cwd, g["root"]))
        status = status_for_root(g["root"]) if (status_for_root and g["root"]) else None
        dirty = {}
        if status and status.get("ok"):
            for f in (status.get("changes") or {}).get("files") or []:
                dirty[f["path"]] = f
            g["branch"] = (status.get("branch") or {}).get("head")
            g["uncommitted_total"] = (status.get("changes") or {}).get("total", 0)
        files = sorted(g["files"].values(), key=lambda f: -f["last_ts"])
        for f in files:
            if status and status.get("ok") and f.get("rel_path"):
                f["still_uncommitted"] = f["rel_path"] in dirty
                if f["still_uncommitted"]:
                    f["evidence"] = DOCUMENTED + "+" + OBSERVED
            else:
                f["still_uncommitted"] = None
        g["file_count"] = len(files)
        g["files"] = files[:max_files]
        g["sessions"] = sorted(s for s in g["sessions"] if s)
        g["profiles"] = sorted(p for p in g["profiles"] if p)
        out.append(g)
    out.sort(key=lambda g: (not g["outside_cwd"], -g["last_ts"]))
    return {"groups": out, "event_count": len(events)}

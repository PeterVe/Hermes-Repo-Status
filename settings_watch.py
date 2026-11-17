"""F3 — changes to Hermes' own settings: skills, memory, cron, config, SOUL, plugins, hooks.

Two sources, kept apart like the rest of the ledger:
  documented  skill_manage / memory / cronjob_* tool calls and file-tool writes into a
              Hermes home (recorded by the post_tool_call hook and the history import)
  observed    a fingerprint scan of the settings files (size + mtime, never content)
              finds created / modified / deleted files no matter who changed them

Secrets are never touched: ``.env``, ``auth.json``, keys and lock files are not in the
watched set at all — not even their size or timestamp.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable

try:
    from . import ledger as _ledger  # type: ignore[import-not-found]
except ImportError:  # flat layout: dashboard/plugin_api.py loads siblings by path, tests use sys.path
    import sys as _sys

    _ledger = _sys.modules.get("repo_status_plugin_ledger")  # type: ignore[assignment]
    if _ledger is None:
        import ledger as _ledger  # type: ignore[no-redef]

MIN_SCAN_INTERVAL = 60.0
SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".venv", "venv", ".cache", ".pytest_cache", "dist", "build"}
SKIP_SUFFIXES = (".lock", ".pyc", ".tmp", ".swp", ".log")
SECRET_NAMES = {".env", "auth.json", "auth.lock", "credentials.json", "token.json"}
ATTRIBUTION_WINDOW = 15 * 60

CATEGORY_LABELS = {
    "skill": "Skills",
    "memory": "Memory",
    "cron": "Scheduled jobs",
    "config": "Configuration",
    "agent": "Agent (SOUL)",
    "plugin": "Plugins",
    "hook": "Hooks",
}

_last_scan: dict[str, float] = {}
_scan_lock = threading.Lock()


def _ok_file(name: str) -> bool:
    low = name.lower()
    return low not in SECRET_NAMES and not low.endswith(SKIP_SUFFIXES) and not low.startswith(".env")


def watched_files(home: Path) -> Iterable[Path]:
    for name in ("config.yaml", "SOUL.md"):
        p = home / name
        if p.is_file():
            yield p
    mem = home / "memories"
    if mem.is_dir():
        for p in mem.glob("*.md"):
            if p.is_file():
                yield p
    jobs = home / "cron" / "jobs.json"
    if jobs.is_file():
        yield jobs
    plugins = home / "plugins"
    if plugins.is_dir():
        for child in plugins.iterdir():
            for m in ("plugin.yaml", "plugin.json"):
                p = child / m
                if p.is_file():
                    yield p
    for sub in ("skills", "hooks"):
        base = home / sub
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in filenames:
                if _ok_file(f):
                    yield Path(dirpath) / f


def fingerprint(home: Path) -> dict[str, tuple[int, int, str, str]]:
    """rel_path → (size, mtime_ns, category, item)."""
    out: dict[str, tuple[int, int, str, str]] = {}
    skill_cache: dict[str, str] = {}
    for p in watched_files(home):
        try:
            st = p.stat()
        except OSError:
            continue
        rel = p.relative_to(home).as_posix()
        parts = rel.split("/")
        if parts[0] == "skills":
            key = str(p.parent)
            if key not in skill_cache:
                skill_cache[key] = _ledger.skill_name_for(p)
            cat, item = "skill", skill_cache[key]
        else:
            hit = _ledger.settings_category(str(p), [home])
            if not hit:
                continue
            cat, item = hit
        out[rel] = (int(st.st_size), int(st.st_mtime_ns), cat, item)
    return out


def scan(target: "_ledger.Ledger", *, homes: list[Path] | None = None, force: bool = False, now: float | None = None) -> dict[str, Any]:
    """Compare every home's settings with the stored fingerprint; record observed changes."""
    now = now or time.time()
    report: dict[str, Any] = {"homes": [], "events": 0, "skipped": False}
    homes = homes if homes is not None else _ledger.hermes_homes()
    root = homes[0] if homes else None
    with _scan_lock:
        key = "|".join(str(h) for h in homes)
        if not force and now - _last_scan.get(key, 0) < MIN_SCAN_INTERVAL:
            report["skipped"] = True
            return report
        _last_scan[key] = now
    target.db_path.parent.mkdir(parents=True, exist_ok=True)
    for home in homes:
        profile = "default" if home == root else home.name
        current = fingerprint(home)
        events = _sync(target, str(home), profile, current, now)
        report["homes"].append({"home": str(home), "profile": profile, "files": len(current), "changes": events})
        report["events"] += events
    return report


def _sync(target: "_ledger.Ledger", home: str, profile: str, current: dict[str, tuple[int, int, str, str]], now: float) -> int:
    with target._lock:
        conn = target._connect()
        try:
            target._ensure(conn)
            stored = {
                r[0]: (r[1], r[2], r[3], r[4])
                for r in conn.execute("SELECT rel_path, size, mtime_ns, category, item FROM settings_files WHERE home = ?", (home,))
            }
            baseline = not stored
            rows: list[dict[str, Any]] = []
            if not baseline:
                for rel, (size, mtime, cat, item) in current.items():
                    old = stored.get(rel)
                    if old is None:
                        rows.append(_row(now, profile, home, rel, "create", cat, item))
                    elif old[0] != size or old[1] != mtime:
                        rows.append(_row(now, profile, home, rel, "modify", cat, item))
                for rel, (_s, _m, cat, item) in stored.items():
                    if rel not in current:
                        rows.append(_row(now, profile, home, rel, "delete", cat, item))
            conn.execute("BEGIN")
            if rows:
                conn.executemany(
                    "INSERT INTO events(ts, profile, session_id, task_id, tool, action, evidence, path, git_root, rel_path, cwd, path_guessed, detail, source, tool_call_id, category, item)"
                    " VALUES(:ts, :profile, NULL, NULL, 'watch', :action, 'observed', :path, NULL, :rel_path, NULL, 0, :rel_path, 'watch', NULL, :category, :item)",
                    rows,
                )
            conn.execute("DELETE FROM settings_files WHERE home = ?", (home,))
            conn.executemany(
                "INSERT INTO settings_files(home, rel_path, size, mtime_ns, category, item) VALUES(?, ?, ?, ?, ?, ?)",
                [(home, rel, s, m, c, i) for rel, (s, m, c, i) in current.items()],
            )
            conn.execute("COMMIT")
            return len(rows)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()


def _row(ts: float, profile: str, home: str, rel: str, action: str, cat: str, item: str) -> dict[str, Any]:
    return {"ts": ts, "profile": profile, "action": action, "path": str(Path(home) / rel), "rel_path": rel, "category": cat, "item": item}


# ── display ──────────────────────────────────────────────────────────────────


def summarize_settings(events: list[dict[str, Any]], *, max_items: int = 50) -> dict[str, Any]:
    """Group settings events by category → item; attribute observed changes to Hermes when a
    documented change of the same item happened within the attribution window."""
    documented: dict[tuple[str, str], list[float]] = {}
    for ev in events:
        if ev.get("evidence") == _ledger.DOCUMENTED and ev.get("category"):
            documented.setdefault((ev["category"], str(ev.get("item") or "")), []).append(ev["ts"])

    cats: dict[str, dict[str, Any]] = {}
    for ev in events:  # newest first
        cat = ev.get("category")
        if not cat:
            continue
        item_name = str(ev.get("item") or "?")
        if cat == "skill" and (item_name.startswith(".") or item_name == "(katalóg skills)"):
            item_name = _ledger.SKILLS_CATALOG_ITEM  # rows recorded before the catalog item existed
        c = cats.setdefault(cat, {"category": cat, "label": CATEGORY_LABELS.get(cat, cat), "items": {}, "last_ts": ev["ts"], "count": 0})
        c["count"] += 1
        it = c["items"].get(item_name)
        if it is None:
            it = c["items"][item_name] = {"item": item_name, "last_ts": ev["ts"], "actions": [], "by_hermes": False, "observed": False,
                                           "unattributed": False, "sessions": set(), "profiles": set(), "details": []}
        if ev["action"] not in it["actions"]:
            it["actions"].append(ev["action"])
        if ev.get("session_id"):
            it["sessions"].add(ev["session_id"])
        if ev.get("profile"):
            it["profiles"].add(ev["profile"])
        if ev.get("evidence") == _ledger.DOCUMENTED:
            it["by_hermes"] = True
        else:
            it["observed"] = True
            times = documented.get((cat, item_name), [])
            if not any(abs(t - ev["ts"]) <= ATTRIBUTION_WINDOW for t in times):
                it["unattributed"] = True
        d = ev.get("detail") or ev.get("rel_path")
        if d and d not in it["details"] and len(it["details"]) < 10:
            it["details"].append(d)

    out = []
    for c in cats.values():
        items = sorted(c["items"].values(), key=lambda i: -i["last_ts"])
        for it in items:
            it["sessions"] = sorted(it["sessions"])
            it["profiles"] = sorted(it["profiles"])
        c["item_count"] = len(items)
        c["items"] = items[:max_items]
        out.append(c)
    order = {k: i for i, k in enumerate(CATEGORY_LABELS)}
    out.sort(key=lambda c: order.get(c["category"], 99))
    return {"categories": out, "event_count": len(events)}

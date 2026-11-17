"""Disk usage of Hermes — read-only overview, nothing is ever deleted or moved.

Hermes builds working copies for bigger tasks in ``<HERMES_HOME>/cache/scratch/<task>``
(reviews, implementations, releases — often with node_modules and build output) and leaves
them behind. This module measures:
  * free space on the drive holding HERMES_HOME
  * size of each top-level area of HERMES_HOME
  * each scratch folder: size, file count, newest change, and — from the repo-status ledger —
    which session/project wrote into it

Walking hundreds of thousands of files takes tens of seconds, so results are cached in
``plugin-shared/repo-status/disk.json`` and refreshed in a background thread.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

STALE_SECONDS = 6 * 3600
SCRATCH_REL = Path("cache") / "scratch"

_scan_lock = threading.Lock()
_running: dict[str, float] = {}


_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: symlinks AND junctions on Windows


def _is_link(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _path_is_link(p: Path) -> bool:
    try:
        return p.is_symlink() or _is_link(p.lstat())
    except OSError:
        return True


def _walk(path: Path) -> tuple[int, int, float]:
    """(bytes, files, newest mtime) — symlinks and junctions are never followed (a junction is not
    a symlink to os.DirEntry before Python 3.12), unreadable parts skipped."""
    total = files = 0
    newest = 0.0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                        if e.is_symlink() or _is_link(st):
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                            continue
                    except OSError:
                        continue
                    total += st.st_size
                    files += 1
                    if st.st_mtime > newest:
                        newest = st.st_mtime
        except OSError:
            continue
    return total, files, newest


def measure(home: Path, *, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    started = time.time()
    du = shutil.disk_usage(home)
    areas = []
    scratch = []
    scratch_root = home / SCRATCH_REL
    try:
        children = sorted(p for p in home.iterdir())
    except OSError:
        children = []
    for child in children:
        if _path_is_link(child):
            continue
        if child.is_dir():
            if child.name == "cache":
                sub_total = sub_files = 0
                sub_newest = 0.0
                for sub in sorted(child.iterdir()) if child.is_dir() else []:
                    if sub.resolve() == scratch_root.resolve() and sub.is_dir():
                        for task in sorted(sub.iterdir()):
                            if task.is_dir() and not _path_is_link(task):
                                if progress:
                                    progress(task.name)
                                b, f, n = _walk(task)
                                scratch.append({"name": task.name, "path": str(task), "size": b, "files": f, "newest": n or None})
                                sub_total, sub_files, sub_newest = sub_total + b, sub_files + f, max(sub_newest, n)
                            elif task.is_file():
                                st = task.stat()
                                sub_total, sub_files = sub_total + st.st_size, sub_files + 1
                    elif sub.is_dir() and not _path_is_link(sub):
                        b, f, n = _walk(sub)
                        sub_total, sub_files, sub_newest = sub_total + b, sub_files + f, max(sub_newest, n)
                    elif sub.is_file():
                        sub_total, sub_files = sub_total + sub.stat().st_size, sub_files + 1
                areas.append({"name": "cache", "path": str(child), "size": sub_total, "files": sub_files, "newest": sub_newest or None})
            else:
                if progress:
                    progress(child.name)
                b, f, n = _walk(child)
                areas.append({"name": child.name, "path": str(child), "size": b, "files": f, "newest": n or None})
        elif child.is_file():
            st = child.stat()
            areas.append({"name": child.name, "path": str(child), "size": st.st_size, "files": 1, "newest": st.st_mtime, "file": True})
    areas.sort(key=lambda a: -a["size"])
    scratch.sort(key=lambda s: -s["size"])
    return {
        "home": str(home),
        "drive": {"total": du.total, "used": du.used, "free": du.free, "free_pct": round(du.free * 100 / du.total, 1) if du.total else None},
        "home_size": sum(a["size"] for a in areas),
        "areas": areas,
        "scratch_root": str(scratch_root),
        "scratch": scratch,
        "scratch_size": sum(s["size"] for s in scratch),
        "scanned_at": time.time(),
        "duration": round(time.time() - started, 1),
    }


def attribute(scratch: list[dict[str, Any]], ledger_db: Path, state_db: Path | None = None) -> None:
    """Add sessions / project folder / first-last write to each scratch folder from the ledger."""
    if not scratch or not ledger_db.exists():
        return
    by_name = {s["name"].lower(): s for s in scratch}
    roots = [(os.path.normcase(os.path.abspath(s["path"])), s["name"].lower()) for s in scratch if s.get("path")]
    marker = os.sep + "cache" + os.sep + "scratch" + os.sep
    info: dict[str, dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(f"file:{ledger_db.as_posix()}?mode=ro", uri=True, timeout=3.0)
        rows = conn.execute("SELECT path, session_id, ts, cwd FROM events WHERE path LIKE ?", (f"%{marker}%",)).fetchall()
        conn.close()
    except sqlite3.Error:
        return
    for path, sid, ts, cwd in rows:
        low = os.path.normcase(os.path.abspath(path or ""))
        # Match the measured directory, not the first cache/scratch segment:
        # Hermes homes and exported fixtures can themselves live in scratch.
        matches = [(len(root), name) for root, name in roots if low == root or low.startswith(root + os.sep)]
        if not matches:
            continue
        name = max(matches)[1]
        d = info.setdefault(name, {"sessions": {}, "first": ts, "last": ts, "cwds": {}})
        d["first"], d["last"] = min(d["first"], ts), max(d["last"], ts)
        if sid:
            d["sessions"][sid] = d["sessions"].get(sid, 0) + 1
        if cwd and marker.lower() not in cwd.lower():
            d["cwds"][cwd] = d["cwds"].get(cwd, 0) + 1
    titles = _titles(state_db, [sid for d in info.values() for sid in d["sessions"]]) if state_db else {}
    for name, d in info.items():
        s = by_name[name]
        top = sorted(d["sessions"].items(), key=lambda kv: -kv[1])
        s["sessions"] = [{"id": sid, "title": titles.get(sid, {}).get("title"), "writes": n} for sid, n in top[:5]]
        s["project_cwd"] = max(d["cwds"].items(), key=lambda kv: kv[1])[0] if d["cwds"] else (titles.get(top[0][0], {}).get("cwd") if top else None)
        s["first_write"], s["last_write"] = d["first"], d["last"]


def _titles(state_db: Path, ids: list[str]) -> dict[str, dict[str, Any]]:
    ids = list(dict.fromkeys(i for i in ids if i))[:500]
    if not ids or not state_db.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{state_db.as_posix()}?mode=ro", uri=True, timeout=3.0)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        if not {"id", "title", "cwd"} <= cols:
            return {}
        marks = ",".join("?" * len(ids))
        out = {r[0]: {"title": r[1], "cwd": r[2]} for r in conn.execute(f"SELECT id, title, cwd FROM sessions WHERE id IN ({marks})", ids)}
        conn.close()
        return out
    except sqlite3.Error:
        return {}


# ── cache + background refresh ───────────────────────────────────────────────


def cache_path(ledger_db: Path) -> Path:
    return ledger_db.parent / "disk.json"


def load_cached(ledger_db: Path) -> dict[str, Any] | None:
    try:
        return json.loads(cache_path(ledger_db).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def refresh(home: Path, ledger_db: Path, state_db: Path | None = None) -> dict[str, Any]:
    """Measure now (blocking), attribute, store the cache. One scan per home at a time."""
    key = str(home)
    with _scan_lock:
        if key in _running:
            raise RuntimeError("scan already running")
        _running[key] = time.time()
    try:
        data = measure(home)
        attribute(data["scratch"], ledger_db, state_db)
        path = cache_path(ledger_db)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return data
    finally:
        with _scan_lock:
            _running.pop(key, None)


def overview(home: Path, ledger_db: Path, state_db: Path | None = None, *, force: bool = False) -> dict[str, Any]:
    """Cached result immediately; starts a background refresh when stale, missing or forced."""
    data = load_cached(ledger_db)
    stale = data is None or force or time.time() - (data.get("scanned_at") or 0) > STALE_SECONDS
    started = False
    if stale:
        with _scan_lock:
            running = str(home) in _running
        if not running:
            threading.Thread(target=_safe_refresh, args=(home, ledger_db, state_db), daemon=True, name="repo-status-disk").start()
            started = True
    out = dict(data or {})
    # free space is cheap: always live
    try:
        du = shutil.disk_usage(home)
        out["drive"] = {"total": du.total, "used": du.used, "free": du.free, "free_pct": round(du.free * 100 / du.total, 1) if du.total else None}
    except OSError:
        pass
    with _scan_lock:
        out["scanning"] = str(home) in _running or started
    out["cached"] = data is not None
    return out


def _safe_refresh(home: Path, ledger_db: Path, state_db: Path | None) -> None:
    try:
        refresh(home, ledger_db, state_db)
    except Exception:
        pass

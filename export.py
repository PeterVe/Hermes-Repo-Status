"""Project exports. Source trees and Hermes databases are always read-only."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import shutil
import socket
import time
import uuid
import zipfile
from datetime import datetime
import importlib.util
import os
import re
import stat
import subprocess
import sys
import threading
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def _module(name):
    key = "_repo_status_export_" + name
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, _ROOT / (name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    mod = sys.modules[key]
    if "_ReadOnlySQLite" in globals() and hasattr(mod, "sqlite3"):
        mod.sqlite3 = _ReadOnlySQLite()
    return mod


gitprobe = _module("gitprobe")
disk_usage = _module("disk_usage")
tr = _module("i18n").tr
_SHARED_KEY = "_repo_status_export_state_v1"
_shared = sys.modules.setdefault(_SHARED_KEY, types.ModuleType(_SHARED_KEY))
if not hasattr(_shared, "lock"):
    _shared.lock = threading.RLock()
    _shared.job = {"ok": True, "state": "idle", "running": False, "step": "", "files": 0,
                   "bytes": 0, "percent": 0, "warnings": [], "error": None}

# Real secret FILES, matched on the file name only. gitprobe's display globs (``*secret*``,
# ``*credential*``) are too broad for a backup: they would drop source code such as
# ``credentials.rs`` or ``secretbox.go`` and, through history, whole repository bundles.
_SECRET = (".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "id_rsa", "id_rsa.*", "id_dsa", "id_ecdsa", "id_ed25519",
           "id_ecdsa.*", "id_ed25519.*", "auth.json", ".git-credentials", ".netrc", "_netrc",
           "credentials.json", "credentials.yaml", "credentials.yml", "secrets.json", "secrets.yaml", "secrets.yml")
_NOT_SECRET = (".env.example", ".env.sample", ".env.template", ".env.dist", "*.pub")
_SKIP = {"node_modules", ".venv", "venv", "bin", "obj", "__pycache__", ".git", "dist", "build"}
MAX_FILES = 200000
MAX_FOLDER_BYTES = 10 * 1024 ** 3


def _safe_name(value):
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "project")).strip("._-")[:80]
    if not text or text.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)), *("LPT" + str(i) for i in range(1, 10))}:
        text = "project_" + text
    return text


def _linked(path):
    """Check every existing component, including Windows junction/reparse points."""
    p = Path(os.path.abspath(path))
    for part in (p, *p.parents):
        try:
            st = part.lstat()
            if stat.S_ISLNK(st.st_mode) or disk_usage._is_link(st):
                return True
        except FileNotFoundError:
            continue
    return False


def _secret(path):
    name = Path(str(path).replace("\\", "/")).name.lower()
    if not name or any(fnmatch.fnmatchcase(name, pat) for pat in _NOT_SECRET):
        return False
    return any(fnmatch.fnmatchcase(name, pat) for pat in _SECRET)


def _git(cwd, *args, check=True, timeout=60):
    if args and args[0] in {"diff", "log", "show"}:
        args = (args[0], "--no-ext-diff", "--no-textconv", *args[1:])
    env = gitprobe._base_env(network=True)
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=5")
    proc = subprocess.run(["git", "--no-optional-locks", "--no-replace-objects", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                           "-c", "protocol.ext.allow=never", "-C", str(cwd), *map(str, args)],
                          env=env, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if check and proc.returncode:
        raise RuntimeError(gitprobe.redact_text(proc.stderr.decode("utf-8", "replace").strip()))
    return proc


def _scan(directories, settings, omitted):
    files, repos = [], []
    output = Path(os.path.abspath(settings["export_dir"]))
    used = set()
    for directory in directories:
        root = Path(directory)
        if not root.is_absolute() or _linked(root) or not root.is_dir():
            raise ValueError("unsafe source directory: " + str(root))
        if root == output or root.is_relative_to(output):
            raise ValueError("source directory is inside export_dir")
        label = _safe_name(root.name)
        stem, i = label, 1
        while label.lower() in used:
            i += 1
            label = f"{stem}_{i}"
        used.add(label.lower())
        probe = _git(root, "rev-parse", "--show-toplevel", check=False)
        git_root = Path(probe.stdout.decode("utf-8", "replace").strip()) if probe.returncode == 0 else None
        if git_root:
            if _linked(git_root) or _linked(git_root / ".git"):
                raise ValueError("unsafe git directory")
            repos.append((git_root, label))
            names = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout.decode("utf-8", "surrogateescape").split("\0")
            candidates = (root / name for name in dict.fromkeys(names) if name)
        else:
            def walk(folder):
                with os.scandir(folder) as entries:
                    for entry in entries:
                        p = Path(entry.path)
                        st = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(st.st_mode) or disk_usage._is_link(st):
                            omitted.append({"path": str(p), "reason": "link"})
                        elif p == output:
                            omitted.append({"path": str(p), "reason": "export_dir"})
                        elif entry.is_dir(follow_symlinks=False):
                            if entry.name not in _SKIP:
                                yield from walk(p)
                        else:
                            yield p
            candidates = walk(root)
        total = 0
        for path in candidates:
            if not path.is_relative_to(root) or ".." in path.relative_to(root).parts:
                raise ValueError("unsafe source path")
            reason = "link" if _linked(path) else "secret" if not settings.get("export_include_secrets") and _secret(path.relative_to(root)) else None
            if path == output or output in path.parents:
                reason = "export_dir"
            if reason:
                omitted.append({"path": str(path), "reason": reason})
                continue
            if not path.exists() or not stat.S_ISREG(path.lstat().st_mode):
                continue
            size = path.stat().st_size
            total += size
            if len(files) >= MAX_FILES or (not git_root and total > MAX_FOLDER_BYTES):
                omitted.append({"path": str(path), "reason": "limit"})
                continue
            files.append((path, "folders/" + label + "/" + path.relative_to(root).as_posix(), size))
    return files, repos


def status():
    import copy
    with _shared.lock:
        return copy.deepcopy(_shared.job)


def _update(progress=None, **fields):
    with _shared.lock:
        _shared.job.update(fields)
        snapshot = status()
    if progress:
        try:
            progress(snapshot)
        except Exception:
            pass  # A display callback must not damage an otherwise valid export.


def _json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2, default=str) + "\n", encoding="utf-8")


def _sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _safe_remove_work(work, export_dir):
    work, output = Path(os.path.abspath(work)), Path(os.path.abspath(export_dir))
    parent = output / "_work"
    marker = work / ".repo-status-export"
    if work.parent != parent or _linked(work) or not marker.is_file() or _linked(marker):
        raise ValueError("unsafe_cleanup")
    if json.loads(marker.read_text(encoding="utf-8")).get("exporter") != "repo-status":
        raise ValueError("unsafe_cleanup")
    shutil.rmtree(work)


def _probe_git(cwd, *args, timeout=30, network=False, check=False):
    proc = _git(cwd, *args, timeout=timeout, check=check)
    return gitprobe.GitResult(proc.returncode, proc.stdout.decode("utf-8", "replace"),
                              proc.stderr.decode("utf-8", "replace").strip())


# A private gitprobe instance keeps every probe on the noninteractive network environment
# without monkey-patching the dashboard's module or another export thread.
gitprobe.run_git = _probe_git


def _bundle_allowed(root, label, settings, warnings):
    if settings.get("export_include_secrets"):
        return True
    # log --all does not visit refs whose peeled targets are trees or blobs,
    # but bundle --all includes them. Refuse those bundles rather than leak
    # uninspectable historical paths through non-commit tags.
    refs = _git(root, "for-each-ref", "--format=%(objecttype)%00%(refname)", check=False)
    unsupported = bool(refs.returncode)
    for line in refs.stdout.decode("utf-8", "replace").splitlines():
        kind, _, ref = line.partition("\0")
        if kind == "tag":
            peeled = _git(root, "cat-file", "-t", ref + "^{}", check=False)
            kind = peeled.stdout.decode("ascii", "replace").strip() if peeled.returncode == 0 else "unsupported"
        if kind != "commit":
            unsupported = True
            break
    if unsupported:
        warnings.append(tr("export.error", "unsupported_ref_bundle_omitted: " + label))
        return False
    history = _git(root, "log", "--all", "--full-history", "-m", "--format=", "--name-only", "-z", "--no-renames", check=False, timeout=180)
    if history.returncode or any(_secret(path.lstrip("\n")) for path in history.stdout.decode("utf-8", "surrogateescape").split("\0") if path):
        warnings.append(tr("export.error", "secret_history_bundle_omitted: " + label))
        return False
    return True


def _collect_git(root, label, payload, work, settings, warnings):
    target = payload / "git" / label
    target.mkdir(parents=True)
    status_data = gitprobe.probe(root, check_remote_enabled=False)
    branches = gitprobe.remote_branches(root, with_prs=bool(settings.get("branches_show_prs", False)), timeout=10)
    _json(target / "status.json", status_data)
    _json(target / "branches.json", branches)
    _json(target / "prs.json", [{**pr, "branch": branch["name"]} for branch in branches.get("branches", []) for pr in branch.get("prs", [])])
    base = "HEAD" if _git(root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0 else gitprobe.EMPTY_TREE
    paths = _git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", base)
    safe = [p for p in paths.stdout.decode("utf-8", "surrogateescape").split("\0") if p and
            (settings.get("export_include_secrets") or not _secret(p))]
    diff = b""
    # Explicit literal pathspecs exclude secret files even when names contain metacharacters.
    for i in range(0, len(safe), 100):
        args = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--binary", base, "--"]
        args += [":(literal)" + p for p in safe[i:i + 100]]
        diff += _git(root, *args).stdout
    (target / "uncommitted.diff").write_bytes(diff)
    bundles = []
    local = target / "local.bundle"
    if _git(root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0 and _bundle_allowed(root, label + "/local.bundle", settings, warnings):
        _git(root, "bundle", "create", str(local), "--all", timeout=180)
        bundles.append((root, local))
    remotes = _git(root, "remote").stdout.decode("utf-8", "replace").splitlines()
    if remotes:
        remote = "origin" if "origin" in remotes else remotes[0]
        url = _git(root, "remote", "get-url", remote).stdout.decode("utf-8", "replace").strip()
        mirror = work / "mirrors" / (label + ".git")
        mirror.parent.mkdir(parents=True, exist_ok=True)
        try:
            _git(work, "clone", "--mirror", "--no-hardlinks", "--", url, str(mirror), timeout=180)
            server = target / "server.bundle"
            if _bundle_allowed(mirror, label + "/server.bundle", settings, warnings):
                _git(mirror, "bundle", "create", str(server), "--all", timeout=180)
                bundles.append((mirror, server))
        except Exception as exc:
            warnings.append(tr("export.error", "server_unavailable: " + gitprobe.redact_text(str(exc))))
    else:
        warnings.append(tr("export.error", "server_unavailable: no_remote"))
    return bundles


def _verify_archive(path, manifest, bundles):
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError("archive_crc")
        expected = {entry["path"]: entry for entry in manifest["files"]}
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(expected) | {"manifest.json"}:
            raise ValueError("archive_file_count")
        if json.loads(archive.read("manifest.json")) != manifest:
            raise ValueError("archive_manifest")
        for name, entry in expected.items():
            if archive.getinfo(name).file_size != entry["size"]:
                raise ValueError("archive_file_size: " + name)
            with archive.open(name) as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != entry["sha256"]:
                raise ValueError("archive_sha256: " + name)
    for root, bundle in bundles:
        _git(root, "bundle", "verify", str(bundle), timeout=180)


def _home(settings):
    if settings.get("hermes_home"):
        return Path(settings["hermes_home"])
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _database_paths(settings):
    home = _home(settings)
    ledger = Path(settings.get("ledger_path") or _module("ledger").default_ledger_path())
    return [home / "projects.db", home / "state.db", ledger,
            Path(settings.get("llm_path") or ledger.parent / "llm.db")]


def _connect_ro(database, *args, **kwargs):
    """Immutable source reads never create WAL/SHM; real jobs read isolated copies."""
    import sqlite3
    from urllib.parse import unquote, urlsplit
    text = str(database)
    if text.startswith("file:"):
        text = unquote(urlsplit(text).path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:", text):
            text = text[1:]
    source = Path(os.path.abspath(text))
    if _linked(source):
        raise sqlite3.OperationalError("unsafe_database")
    copied = getattr(_shared, "read_copies", {}).get(os.path.normcase(str(source)))
    target = Path(copied) if copied else source
    kwargs["uri"] = True
    conn = sqlite3.connect(target.as_uri() + "?mode=ro" + ("" if copied else "&immutable=1"), *args, **kwargs)
    conn.execute("PRAGMA query_only=1")
    return conn


class _ReadOnlySQLite:
    connect = staticmethod(_connect_ro)

    def __getattr__(self, name):
        import sqlite3
        return getattr(sqlite3, name)


def _stage_databases(settings, work):
    """Copy main+WAL without ever opening a source database through SQLite."""
    copies = {}
    for index, source in enumerate(_database_paths(settings)):
        if not source.is_file() or _linked(source):
            continue
        target = work / "metadata" / str(index) / source.name
        target.parent.mkdir(parents=True)
        wal = source.with_name(source.name + "-wal")
        def signature(path):
            if not path.exists():
                return None
            if _linked(path):
                raise ValueError("unsafe_database_sidecar")
            info = path.stat()
            return info.st_size, info.st_mtime_ns, info.st_ino
        before = signature(source), signature(wal)
        shutil.copyfile(source, target)
        if before[1] is not None:
            shutil.copyfile(wal, target.with_name(target.name + "-wal"))
        if before != (signature(source), signature(wal)):
            raise ValueError("database_changed_during_snapshot: " + source.name)
        copies[os.path.normcase(os.path.abspath(source))] = str(target)
    _shared.read_copies = copies


def _rows(db, table, columns, warnings):
    """Schema-tolerant, allowlisted metadata reads. Never open a migrating store."""
    import sqlite3
    if not db.is_file() or _linked(db):
        return []
    conn = None
    try:
        conn = _connect_ro(db.absolute().as_uri() + "?mode=ro", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=1")
        available = {row[1] for row in conn.execute('PRAGMA table_info("' + table + '")')}
        selected = [name for name in columns if name in available]
        if not selected:
            return []
        sql = "SELECT " + ",".join('"' + name + '"' for name in selected) + ' FROM "' + table + '" LIMIT ' + str(MAX_FILES + 1)
        rows = [dict(row) for row in conn.execute(sql)]
        if len(rows) > MAX_FILES:
            warnings.append(tr("export.error", "metadata_limit: " + table))
        return rows[:MAX_FILES]
    except sqlite3.Error as exc:
        warnings.append(tr("export.error", "metadata_unavailable: " + str(exc)))
        return []
    finally:
        if conn is not None:
            conn.close()


def _inside(path, roots):
    if not path:
        return False
    try:
        p = Path(os.path.abspath(path))
        return any(p == Path(os.path.abspath(root)) or p.is_relative_to(Path(os.path.abspath(root))) for root in roots)
    except (TypeError, ValueError):
        return False


def _resolve(settings, project, directories, cwd, warnings):
    home = _home(settings)
    registry = _rows(home / "projects.db", "projects", ("id", "name", "slug", "primary_path", "description"), warnings)
    folders = _rows(home / "projects.db", "project_folders", ("project_id", "path"), warnings)
    candidates = []
    for item in registry:
        paths = [row["path"] for row in folders if row.get("project_id") == item.get("id")]
        if item.get("primary_path") and item["primary_path"] not in paths:
            paths.insert(0, item["primary_path"])
        explicit = project and any(project.get(key) and project.get(key) == item.get(key) for key in ("id", "name", "slug"))
        matches = [path for path in paths if _inside(cwd, [path])]
        if explicit or (project is None and matches):
            candidates.append((max(map(len, matches), default=0), item, paths))
    if candidates:
        _, registered, paths = max(candidates, key=lambda row: row[0])
        project = {**registered, **(project or {})}
        directories = directories or paths
    elif project:
        directories = directories or project.get("directories") or project.get("folders")
        if directories:
            directories = [p.get("path") if isinstance(p, dict) else p for p in directories]
    if not directories:
        source = Path(cwd or os.getcwd())
        root = _git(source, "rev-parse", "--show-toplevel", check=False)
        directories = [root.stdout.decode("utf-8", "replace").strip()] if root.returncode == 0 else [str(source)]
    project = project or {"name": Path(directories[0]).name}
    project = {key: value for key, value in project.items() if key in {"id", "name", "slug", "primary_path", "description", "profile"}}
    return project, list(dict.fromkeys(map(str, directories)))


def _metadata(settings, project, directories, warnings):
    home = _home(settings)
    ledger_mod, llm_mod = _module("ledger"), _module("llm_log")
    ledger_path = Path(settings.get("ledger_path") or ledger_mod.default_ledger_path())
    state_path = home / "state.db"
    sessions = _rows(state_path, "sessions", ("id", "title", "source", "platform", "started_at", "ended_at", "cwd", "project_id", "model", "billing_provider", "input_tokens", "output_tokens"), warnings)
    sessions = [s for s in sessions if _inside(s.get("cwd"), directories) or
                (project.get("id") and s.get("project_id") == project["id"])]
    ids = {s["id"] for s in sessions}
    # Bypass write-capable constructors; the invoked methods open mode=ro connections.
    ledger = ledger_mod.Ledger.__new__(ledger_mod.Ledger)
    ledger.db_path = ledger_path
    touched, changed = [], []
    if ledger_path.is_file() and not _linked(ledger_path):
        try:
            allowed = {"id", "ts", "profile", "session_id", "task_id", "tool", "action", "evidence", "path", "git_root", "rel_path", "cwd", "source", "category", "item"}
            scoped = lambda row: row.get("session_id") in ids or _inside(row.get("cwd"), directories) or _inside(row.get("path"), directories)
            touched = [{k: v for k, v in row.items() if k in allowed} for row in ledger.query(limit=MAX_FILES) if scoped(row)]
            changed = [{k: v for k, v in row.items() if k in allowed} for row in ledger.query(settings_only=True, limit=MAX_FILES) if scoped(row)]
            ids.update(row["session_id"] for row in touched if row.get("session_id"))
        except Exception as exc:
            warnings.append(tr("export.error", "ledger_metadata: " + str(exc)))
    llm_path = Path(settings.get("llm_path") or llm_mod.default_db_path(ledger_path))
    log = llm_mod.LlmLog.__new__(llm_mod.LlmLog)
    log.db_path = llm_path
    calls, meta = [], {}
    if llm_path.is_file() and not _linked(llm_path):
        try:
            columns = set(llm_mod._COLUMNS) - {"error_message", "base_url"}
            calls = [{k: v for k, v in row.items() if k in columns} for row in log.calls(limit=MAX_FILES) if row.get("session_id") in ids]
            for offset in range(0, len(ids), 500):
                meta.update(log.session_meta(sorted(ids)[offset:offset + 500]))
        except Exception as exc:
            warnings.append(tr("export.error", "llm_metadata: " + str(exc)))
    usage = {}
    workspace = _module("workspace")
    for offset in range(0, len(ids), 500):
        usage.update(workspace.usage_history(state_path, sorted(ids)[offset:offset + 500]))
    for session in sessions:
        sid = session["id"]
        session.update(meta.get(sid, {}))
        session["providers"] = workspace.merge_providers(session.get("providers"), usage.get(sid))
    return {"sessions": sessions, "touched": touched, "settings": changed, "llm-calls": calls, "scratch": []}


def _scratch(settings, metadata, directories, omitted, warnings):
    disk_usage.sqlite3 = _ReadOnlySQLite()
    home = _home(settings)
    ledger_path = Path(settings.get("ledger_path") or _module("ledger").default_ledger_path())
    cached = disk_usage.load_cached(ledger_path) if not _linked(disk_usage.cache_path(ledger_path)) else None
    scratch = (cached or {}).get("scratch", [])
    if not isinstance(scratch, list):
        scratch = []
    scratch = [dict(row) for row in scratch if isinstance(row, dict) and row.get("path")]
    disk_usage.attribute(scratch, ledger_path, home / "state.db")
    ids = {s["id"] for s in metadata["sessions"]} | {s.get("session_id") for s in metadata["touched"]}
    selected, files, total = [], [], 0
    cap = max(0, int(settings.get("export_scratch_cap_mb", 500))) * 1024 ** 2
    for row in scratch:
        if not (_inside(row.get("project_cwd"), directories) or any(s.get("id") in ids for s in row.get("sessions", []))):
            continue
        path = Path(row["path"])
        if path.parent != home / "cache" / "scratch" or _linked(path) or not path.is_dir():
            omitted.append({"path": str(path), "reason": "unsafe_scratch"})
            continue
        size, count, newest = disk_usage._walk(path)
        item = {"name": path.name, "path": str(path), "size": size, "files": count,
                "project_cwd": row.get("project_cwd"), "included": False}
        selected.append(item)
        reason = "scratch_not_included" if not settings.get("export_include_scratch") else "scratch_cap" if total + size > cap else None
        if reason:
            omitted.append({"path": str(path), "reason": reason})
            continue
        copied, _ = _scan([str(path)], settings, omitted)
        for source, relative, length in copied:
            files.append((source, "hermes/work/" + relative.removeprefix("folders/"), length))
        total += size
        item["included"] = True
    metadata["scratch"] = selected
    return files


def _acquire_lock(output):
    """OS-owned advisory lock: persistent file, automatically released on process exit."""
    path = output / ".repo-status-export.lock"
    if _linked(path):
        raise ValueError("unsafe_lock")
    stream = path.open("a+b")
    try:
        if not path.stat().st_size:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        stream.close()
        raise ValueError("export_busy") from exc
    return stream


def _estimate(files, repos, settings, metadata):
    estimate = sum(f[2] for f in files) + len(json.dumps(metadata, default=str).encode("utf-8"))
    for db in dict.fromkeys(_database_paths(settings)):
        for source in (db, db.with_name(db.name + "-wal")):
            if source.is_file() and not _linked(source):
                estimate += source.stat().st_size
    for root in dict.fromkeys(root for root, _ in repos):
        objects = _git(root, "count-objects", "-v", check=False).stdout.decode("ascii", "replace")
        sizes = dict(line.split(": ", 1) for line in objects.splitlines() if ": " in line)
        estimate += 3 * 1024 * (int(sizes.get("size", 0)) + int(sizes.get("size-pack", 0)))
    return estimate


def _check_space(output, estimate, progress):
    ancestor = output
    while not ancestor.exists():
        ancestor = ancestor.parent
    free = shutil.disk_usage(ancestor).free
    required = 2 * estimate + 1024 ** 3
    _update(progress, estimated_bytes=estimate, free_bytes=free, required_bytes=required)
    if free < required:
        raise ValueError(f"insufficient_space: free={free}, required={required}, estimated={estimate}")


def _run(settings, project, directories, dry_run, progress, cwd=None):
    work, lock = None, None
    _shared.read_copies = {}
    requested_project, requested_directories = project, directories
    try:
        output = Path(settings["export_dir"])
        if not output.is_absolute() or _linked(output):
            raise ValueError("unsafe_export_dir")
        omitted, warnings = [], []
        for db in _database_paths(settings):
            wal = db.with_name(db.name + "-wal")
            if wal.is_file() and not _linked(wal) and wal.stat().st_size:
                warnings.append(tr("export.error", "estimate_immutable_snapshot_ignores_wal: " + db.name))
        project, directories = _resolve(settings, project, directories, cwd or os.getcwd(), warnings)
        metadata = _metadata(settings, project, directories, warnings)
        files, repos = _scan(directories, settings, omitted)
        files.extend(_scratch(settings, metadata, directories, omitted, warnings))
        estimate = _estimate(files, repos, settings, metadata)
        _check_space(output, estimate, progress)
        _update(progress, estimated_bytes=estimate, files=len(files), omitted=omitted, project=project,
                warnings=warnings, export_dir=str(output), dry_run=dry_run)
        if dry_run:
            _update(progress, state="dry_run", running=False, percent=100)
            return
        output.mkdir(parents=True, exist_ok=True)
        lock = _acquire_lock(output)
        work_parent = output / "_work"
        if _linked(work_parent):
            raise ValueError("unsafe_work_dir")
        work_parent.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        name = _safe_name(project.get("name") or project.get("id"))
        pc = _safe_name(os.environ.get("COMPUTERNAME") or socket.gethostname())
        suffix = ""
        for i in range(10000):
            suffix = "" if i == 0 else "_" + str(i)
            work = work_parent / (name + "_" + stamp + suffix)
            final = output / (name + "_" + stamp + suffix + "_" + pc + ".zip")
            part = final.with_suffix(".zip.part")
            if work.exists() or final.exists() or part.exists():
                continue
            work.mkdir()
            break
        else:
            raise ValueError("export_name_collision")
        _json(work / ".repo-status-export", {"exporter": "repo-status", "token": uuid.uuid4().hex})
        _stage_databases(settings, work)
        project, directories = _resolve(settings, requested_project, requested_directories, cwd or os.getcwd(), warnings)
        metadata = _metadata(settings, project, directories, warnings)
        omitted.clear()
        files, repos = _scan(directories, settings, omitted)
        files.extend(_scratch(settings, metadata, directories, omitted, warnings))
        _check_space(output, _estimate(files, repos, settings, metadata), progress)
        resolved_name = _safe_name(project.get("name") or project.get("id"))
        if resolved_name != name:
            for i in range(10000):
                suffix = "" if i == 0 else "_" + str(i)
                final = output / (resolved_name + "_" + stamp + suffix + "_" + pc + ".zip")
                part = final.with_suffix(".zip.part")
                if not final.exists() and not part.exists() and not _linked(final) and not _linked(part):
                    break
            else:
                raise ValueError("export_name_collision")
        _update(progress, project=project)
        payload = work / "payload"
        payload.mkdir()
        _update(progress, work_dir=str(work), step="collect", percent=5, files=0, bytes=0)
        copied = 0
        for index, (source, relative, size) in enumerate(files, 1):
            if _linked(source) or not source.is_file():
                raise ValueError("source_changed: " + str(source))
            target = payload / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target, follow_symlinks=False)
            copied += target.stat().st_size
            _update(progress, files=index, bytes=copied, percent=5 + int(index / max(1, len(files)) * 45))
        bundles = []
        for root, label in repos:
            bundles.extend(_collect_git(root, label, payload, work, settings, warnings))
        _json(payload / "hermes" / "project.json", {**project, "directories": list(map(str, directories))})
        home = _home(settings)
        _json(payload / "hermes" / "env.json", _module("env_vars").snapshot(os.environ, home))
        for kind, data in metadata.items():
            _json(payload / "hermes" / (kind + ".json"), data)
        (payload / "README.txt").write_text(tr("export.title") + "\n\n" + tr("export.archive") + ":\n" +
                                             "git clone git/<repo>/server.bundle <repo>\n" +
                                             "git clone git/<repo>/local.bundle <repo>\n" +
                                             "git -C <repo> apply ../git/<repo>/uncommitted.diff\n\n" +
                                             tr("export.directory") + ": folders/<folder>/ -> <folder>/\n", encoding="utf-8")
        entries = [{"path": p.relative_to(payload).as_posix(), "size": p.stat().st_size, "sha256": _sha(p)}
                   for p in sorted(payload.rglob("*")) if p.is_file()]
        try:
            from hermes_cli import __version__ as hermes_version
        except (ImportError, AttributeError):
            from importlib.metadata import PackageNotFoundError, version
            try:
                hermes_version = version("hermes-agent")
            except PackageNotFoundError:
                hermes_version = None
                warnings.append(tr("export.error", "hermes_version_unavailable"))
        manifest = {"exporter": "repo-status", "format_version": 1, "plugin_version": "0.6.0",
                    "hermes_version": hermes_version, "project": project, "pc": pc, "created_at": datetime.now().astimezone().isoformat(),
                    "files": entries, "manifest_excludes": ["manifest.json"], "omitted": omitted, "warnings": warnings, "errors": []}
        _json(payload / "manifest.json", manifest)
        _update(progress, step="archive", percent=75)
        with zipfile.ZipFile(part, "x", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for entry in entries:
                archive.write(payload / entry["path"], entry["path"])
            archive.write(payload / "manifest.json", "manifest.json")
        _update(progress, step="verify", percent=90)
        _verify_archive(part, manifest, bundles)
        part.rename(final)
        _update(progress, step="cleanup", percent=98, archive=str(final), warnings=warnings)
        _safe_remove_work(work, output)
        keep = max(0, int(settings.get("export_keep_last", 0)))
        if keep:
            for entry in list_exports(settings, project)["exports"][keep:]:
                old = Path(entry["path"])
                same = entry["project"].get("id") == project["id"] if project.get("id") else entry["project"].get("name") == project.get("name")
                if same and old != final and old.parent == output and not _linked(old):
                    old.unlink()
        _update(progress, state="completed", running=False, percent=100, message=tr("export.state.completed"))
    except Exception as exc:
        _update(progress, ok=False, state="error", running=False, error=tr("export.error", gitprobe.redact_text(str(exc))),
                work_dir=str(work) if work else None)
    finally:
        _shared.read_copies = {}
        if lock:
            lock.close()
        with _shared.lock:
            _shared.worker_busy = False


def resolve_project(settings, project=None, directories=None, cwd=None):
    warnings = []
    try:
        project, directories = _resolve(settings, project, directories, cwd or os.getcwd(), warnings)
        return {"ok": True, "project": project, "directories": directories, "warnings": warnings}
    except Exception as exc:
        return {"ok": False, "error": tr("export.error", str(exc)), "warnings": warnings}


def start(settings, project=None, directories=None, cwd=None, dry_run=False, background=True, progress=None):
    if (not isinstance(settings, dict) or (project is not None and not isinstance(project, dict)) or
            (directories is not None and (not isinstance(directories, (list, tuple)) or
             any(not isinstance(p, str) or not Path(p).is_absolute() for p in directories))) or
            (cwd is not None and (not isinstance(cwd, str) or not Path(cwd).is_absolute())) or
            not isinstance(dry_run, bool)):
        return {"ok": False, "state": "error", "running": False, "error": tr("export.invalid_request")}
    if not settings.get("export_dir"):
        return {"ok": False, "state": "error", "running": False, "error": tr("export.disabled"), "message": tr("export.disabled")}
    with _shared.lock:
        if _shared.job.get("running") or getattr(_shared, "worker_busy", False):
            return {**status(), "ok": False, "error": tr("export.error", "export_busy")}
        _shared.worker_busy = True
        _shared.job = {"ok": True, "state": "running", "running": True, "step": "estimate", "files": 0,
                       "bytes": 0, "percent": 0, "warnings": [], "error": None, "dry_run": dry_run,
                       "message": tr("export.started")}
    args = (dict(settings), dict(project) if project is not None else None,
            list(directories) if directories is not None else None, dry_run, progress, cwd)
    if background:
        threading.Thread(target=_run, args=args, daemon=True, name="repo-status-export").start()
    else:
        _run(*args)
    return status()


def list_exports(settings, project=None):
    output = Path(settings.get("export_dir") or ".")
    result = {"ok": True, "enabled": bool(settings.get("export_dir")), "export_dir": str(output), "exports": []}
    if not result["enabled"] or not output.is_dir() or _linked(output):
        return result
    name = _safe_name(project.get("name") or project.get("id")) if isinstance(project, dict) else _safe_name(project) if project else None
    pattern = re.compile(r"^(.+)_\d{4}-\d{2}-\d{2}_\d{4}(?:_\d+)?_[A-Za-z0-9._-]+\.zip$")
    for path in output.iterdir():
        if _linked(path) or not path.is_file() or not pattern.fullmatch(path.name):
            continue
        if name and not path.name.startswith(name + "_"):
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("exporter") != "repo-status":
                continue
            if name and _safe_name(manifest["project"].get("name") or manifest["project"].get("id")) != name:
                continue
            info = path.stat()
            result["exports"].append({"name": path.name, "path": str(path), "size": info.st_size, "mtime": info.st_mtime,
                                      "project": manifest["project"], "created_at": manifest["created_at"]})
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            continue
    result["exports"].sort(key=lambda entry: (entry["mtime"], entry["name"]), reverse=True)
    return result

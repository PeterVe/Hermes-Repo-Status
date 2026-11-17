"""Backend for the Repo Status desktop panel — mounted at /api/plugins/repo-status/.

Read-only. Shares the probe with the agent half by loading ``../gitprobe.py`` by
file path (this module is imported standalone, not as part of the plugin package).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

PLUGIN_ID = "repo-status"
router = APIRouter()


def _load_sibling(stem: str):
    name = f"repo_status_plugin_{stem}"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent.parent / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gitprobe = _load_sibling("gitprobe")
ledger = _load_sibling("ledger")
settings_watch = _load_sibling("settings_watch")  # resolves ledger via sys.modules
llm_log = _load_sibling("llm_log")
workspace = _load_sibling("workspace")
honcho_status = _load_sibling("honcho_status")
disk_usage = _load_sibling("disk_usage")
plugin_i18n = _load_sibling("i18n")


@router.get("/i18n")
async def i18n_catalogs() -> dict[str, Any]:
    """Every language catalog (flat keys, positional {0} placeholders) for the Desktop panel."""
    return {"languages": list(plugin_i18n.LANGUAGES), "catalogs": plugin_i18n.all_catalogs()}
env_vars = _load_sibling("env_vars")


def _resolved_env() -> dict[str, str]:
    out = {"HERMES_PROFILE": ledger.current_profile()}
    try:
        from hermes_constants import get_hermes_home

        out["HERMES_HOME"] = str(get_hermes_home())
    except Exception:
        out["HERMES_HOME"] = str(ledger.hermes_root())
    return out


@router.get("/env")
async def env() -> dict[str, Any]:
    """HERMES_* / TERMINAL_* variables of the gateway process (no secrets) with Hermes' own docs."""
    import os

    def run() -> dict[str, Any]:
        data = env_vars.snapshot(os.environ, ledger.hermes_root(), resolved=_resolved_env())
        data["process"] = "gateway"
        return data

    return await run_in_threadpool(run)


@router.get("/disk")
async def disk(refresh: bool = False) -> dict[str, Any]:
    """Hermes disk usage (read-only): cached scan, refreshed in the background when stale."""
    root = ledger.hermes_root()
    return await run_in_threadpool(
        lambda: disk_usage.overview(root, ledger.default_ledger_path(), root / "state.db", force=refresh)
    )


def _config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        return load_config_readonly() or {}
    except Exception:
        return {}


@router.get("/honcho")
async def honcho(check: bool = False, days: float = 7) -> dict[str, Any]:
    """Honcho memory: active provider?, settings (no secrets), optional anonymous reachability check,
    and Honcho events from Hermes' logs."""
    return await run_in_threadpool(lambda: honcho_status.status(ledger.hermes_root(), _config(), check=check, days=days))


@router.get("/ls")
async def ls(path: str) -> dict[str, Any]:
    """F5: one folder level (folders first) for the Files section."""
    return await run_in_threadpool(workspace.list_dir, path)


@router.post("/channels")
async def channels(body: dict) -> dict[str, Any]:
    """F5: messaging channels whose Hermes channel prompt names the project."""
    body = body or {}
    folders = [str(f) for f in body.get("folders") or [] if isinstance(f, str)][:10]
    found = workspace.project_channels(_config(), name=body.get("name"), project_id=body.get("id"), folders=folders)
    return {"channels": found}


@router.post("/sessions-meta")
async def sessions_meta(body: dict) -> dict[str, Any]:
    """F5: per session — context fill (latest call) and providers/models (F4 log + Hermes usage table)."""
    ids = [str(s) for s in (body or {}).get("ids") or [] if isinstance(s, str)][:200]

    def run() -> dict[str, Any]:
        log = llm_log.LlmLog(llm_log.default_db_path(ledger.default_ledger_path()))
        live = log.session_meta(ids)
        hist = workspace.usage_history(ledger.hermes_root() / "state.db", ids)
        out = {}
        for sid in ids:
            m = live.get(sid, {})
            providers = workspace.merge_providers(m.get("providers"), hist.get(sid))
            if providers or m.get("context"):
                out[sid] = {"context": m.get("context"), "providers": providers}
        return out

    return {"sessions": await run_in_threadpool(run)}


def _settings() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        s = (((cfg.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}).get("settings") or {}
    except Exception:
        s = {}

    def num(key: str, default: float) -> float:
        try:
            return max(0.0, float(s.get(key, default)))
        except (TypeError, ValueError):
            return default

    host_kinds = s.get("host_kinds") if isinstance(s.get("host_kinds"), dict) else {}
    work_dirs = s.get("work_dirs", list(gitprobe.DEFAULT_WORK_DIRS))
    if isinstance(work_dirs, str):
        work_dirs = work_dirs.replace(";", ",").split(",")
    if not isinstance(work_dirs, (list, tuple)):
        work_dirs = list(gitprobe.DEFAULT_WORK_DIRS)
    return {
        "work_dirs": tuple(str(w).strip() for w in work_dirs if str(w).strip()),
        "check_remote": bool(s.get("check_remote", True)),
        "branches_show_prs": bool(s.get("branches_show_prs", True)),
        "remote_cache_seconds": num("remote_cache_seconds", gitprobe.DEFAULT_REMOTE_CACHE_SECONDS),
        "remote_timeout_seconds": num("remote_timeout_seconds", gitprobe.DEFAULT_REMOTE_TIMEOUT),
        "file_list_cap": int(num("file_list_cap", gitprobe.DEFAULT_FILE_CAP)),
        "host_kinds": host_kinds,
        "show_chip": bool(s.get("show_chip", True)),
        "export_dir": str(s.get("export_dir") or "").strip(),
        "export_include_secrets": bool(s.get("export_include_secrets", False)),
        "export_include_scratch": bool(s.get("export_include_scratch", False)),
        "export_scratch_cap_mb": int(num("export_scratch_cap_mb", 500)),
        "export_keep_last": int(num("export_keep_last", 0)),
    }


def _check_cwd(cwd: str) -> Path:
    if not cwd or not cwd.strip():
        raise HTTPException(status_code=400, detail="cwd is required")
    p = Path(cwd.strip())
    if not p.is_absolute():
        raise HTTPException(status_code=400, detail="cwd must be absolute")
    return p


def _exporter():
    return _load_sibling("export")


@router.post("/export")
async def export_start(body: dict) -> dict[str, Any]:
    """Start one background job; settings, never the request, control output policy."""
    body = body or {}
    project = body.get("project")
    directories = body.get("directories")
    cwd = body.get("cwd")
    if project is not None and not isinstance(project, dict):
        raise HTTPException(status_code=400, detail=plugin_i18n.tr("export.invalid_request"))
    if directories is not None and (not isinstance(directories, list) or
                                    any(not isinstance(item, str) or not Path(item).is_absolute() for item in directories)):
        raise HTTPException(status_code=400, detail=plugin_i18n.tr("export.invalid_request"))
    if cwd is not None and (not isinstance(cwd, str) or not Path(cwd).is_absolute()):
        raise HTTPException(status_code=400, detail=plugin_i18n.tr("export.invalid_request"))
    if not isinstance(body.get("dry_run", False), bool):
        raise HTTPException(status_code=400, detail=plugin_i18n.tr("export.invalid_request"))
    return await run_in_threadpool(lambda: _exporter().start(
        _settings(), project=project, directories=directories, cwd=cwd, dry_run=body.get("dry_run", False)))


@router.get("/export/status")
async def export_status() -> dict[str, Any]:
    settings = _settings()
    result = await run_in_threadpool(lambda: _exporter().status())
    return {**result, "enabled": bool(settings["export_dir"]), "export_dir": settings["export_dir"]}


@router.get("/export/list")
async def export_list(project: str = "") -> dict[str, Any]:
    return await run_in_threadpool(lambda: _exporter().list_exports(_settings(), project=project or None))


@router.get("/settings")
def settings() -> dict[str, Any]:
    s = _settings()
    return {"show_chip": s["show_chip"], "check_remote": s["check_remote"]}


def _touched_sync(session_id: str | None, since: float | None, cwd: str | None) -> dict[str, Any]:
    events = ledger.Ledger(ledger.default_ledger_path()).query(session_id=session_id, since=since)
    cache: dict[str, Any] = {}

    def status_for_root(root: str):
        if root not in cache:
            cache[root] = gitprobe.probe(root, check_remote_enabled=False, file_cap=2000, work_dirs=())
        return cache[root]

    return ledger.summarize_events(events, cwd=cwd, status_for_root=status_for_root)


@router.get("/llm")
async def llm(hours: float = 24, session: str = "") -> dict[str, Any]:
    """F4: provider calls — totals, per provider/model, top sessions, recent errors."""
    import time

    def run() -> dict[str, Any]:
        log = llm_log.LlmLog(llm_log.default_db_path(ledger.default_ledger_path()))
        sid = session.strip() or None
        data = llm_log.summarize_calls(log.calls(since=None if sid else time.time() - max(0.1, hours) * 3600, session_id=sid))
        try:
            from hermes_cli.config import load_config_readonly

            s = (((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID, {}).get("settings") or {}
        except Exception:
            s = {}
        data["capture_content"] = bool(s.get("llm_capture_content", False))
        data["enabled"] = bool(s.get("llm_log", True))
        return data

    return await run_in_threadpool(run)


@router.get("/hermes-settings")
async def hermes_settings(hours: float = 168, force: bool = False) -> dict[str, Any]:
    """F3: changes to skills / memory / cron / config / SOUL / plugins / hooks."""
    import time

    def run() -> dict[str, Any]:
        led = ledger.Ledger(ledger.default_ledger_path())
        scan = settings_watch.scan(led, force=force)
        data = settings_watch.summarize_settings(led.query(since=time.time() - max(1.0, hours) * 3600, settings_only=True, limit=20000))
        data["scan"] = {"skipped": scan["skipped"], "changes": scan["events"]}
        return data

    return await run_in_threadpool(run)


@router.post("/folders")
async def folders(body: dict) -> dict[str, Any]:
    """For each project folder: does it exist, and which git repo (if any) holds it."""
    paths = [str(p) for p in (body or {}).get("paths") or [] if isinstance(p, str) and p.strip()][:20]

    def probe() -> list[dict[str, Any]]:
        out = []
        for raw in paths:
            p = Path(raw)
            exists = p.exists()
            out.append({"path": raw, "exists": exists, "git_root": ledger.git_root_of(p) if exists else None})
        return out

    return {"folders": await run_in_threadpool(probe)}


@router.get("/touched")
async def touched(session: str = "", hours: float = 24, cwd: str = "") -> dict[str, Any]:
    """Where Hermes wrote: one session (``session=<id>``) or all sessions in the last ``hours``."""
    import time

    session_id = session.strip() or None
    since = None if session_id else time.time() - max(0.1, hours) * 3600
    data = await run_in_threadpool(_touched_sync, session_id, since, cwd.strip() or None)
    data["scope"] = {"session": session_id, "hours": None if session_id else hours}
    return data


@router.get("/remote")
async def remote(cwd: str, fresh: bool = False) -> dict[str, Any]:
    path = _check_cwd(cwd)
    s = _settings()
    if not s["check_remote"]:
        return {"status": "skipped", "checked_at": None}
    return await run_in_threadpool(
        gitprobe.probe_remote,
        path,
        fresh=fresh,
        remote_timeout=s["remote_timeout_seconds"],
        remote_cache_seconds=s["remote_cache_seconds"],
    )


@router.get("/branches")
async def branches(cwd: str, fresh: bool = False) -> dict[str, Any]:
    path = _check_cwd(cwd)
    s = _settings()
    if not s["check_remote"]:
        return {"ok": True, "status": "skipped", "branches": []}
    return await run_in_threadpool(
        gitprobe.remote_branches,
        path,
        fresh=fresh,
        timeout=s["remote_timeout_seconds"],
        cache_seconds=s["remote_cache_seconds"],
        host_kinds=s["host_kinds"],
        with_prs=s["branches_show_prs"],
    )


@router.get("/status")
async def status(cwd: str, fresh: bool = False, remote: bool = True) -> dict[str, Any]:
    path = _check_cwd(cwd)
    s = _settings()
    return await run_in_threadpool(
        gitprobe.probe,
        path,
        check_remote_enabled=s["check_remote"] and remote,
        fresh=fresh,
        host_kinds=s["host_kinds"],
        file_cap=s["file_list_cap"],
        remote_timeout=s["remote_timeout_seconds"],
        remote_cache_seconds=s["remote_cache_seconds"],
        work_dirs=s["work_dirs"],
    )

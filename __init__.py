"""repo-status — read-only overview of the git repository a Hermes session works in.

Registers:
  * tool ``repo_status``      — the model checks repo, remote, git user, branch, freshness, changes
  * slash command ``/repo``   — the same as text in CLI, Desktop, Discord, Telegram …
  * CLI ``hermes repo-status`` — the same in a terminal, plus ``--diff`` (full diff, terminal only)
  * F2: hook ``post_tool_call`` → shared ledger of where Hermes wrote (ledger.py);
    tool ``hermes_touched_paths``, ``/repo touched``, ``hermes repo-status --touched``

The Desktop panel lives in ``desktop/plugin.js`` and reads ``dashboard/plugin_api.py``.
Nothing here writes to a repository; the ledger stores paths only, never file content.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import disk_usage, env_vars, gitprobe, history_import, honcho_status, ledger, llm_log, render, settings_watch
from .i18n import tr


def _env_view(process: str) -> dict[str, Any]:
    resolved = {"HERMES_PROFILE": ledger.current_profile()}
    try:
        from hermes_constants import get_hermes_home

        resolved["HERMES_HOME"] = str(get_hermes_home())
    except Exception:
        resolved["HERMES_HOME"] = str(ledger.hermes_root())
    data = env_vars.snapshot(os.environ, ledger.hermes_root(), resolved=resolved)
    data["process"] = process
    return data

logger = logging.getLogger(__name__)

PLUGIN_ID = "repo-status"
TOOL_FILE_CAP = 100

REPO_STATUS_SCHEMA = {
    "name": "repo_status",
    "description": (
        "Read-only overview of a local git repository: repo root and worktrees, remotes "
        "(GitHub / GitLab / other, credentials redacted), the git user identity and where it "
        "is configured, the current branch and upstream, the last commit, whether the branch "
        "matches the server (checked with `git ls-remote`, never fetch), and the uncommitted "
        "files with +/- line counts. Use it before committing or pushing, when the user asks "
        "which repo/branch you are in or what is uncommitted, and to check another folder you "
        "wrote files in. It never changes the repository."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Folder or file inside the repository. Defaults to the session's working directory.",
            },
            "check_remote": {
                "type": "boolean",
                "description": "Compare with the server via git ls-remote (default: the plugin setting, on).",
            },
            "fresh": {
                "type": "boolean",
                "description": "Ignore the short cache of the server check.",
            },
        },
        "required": [],
    },
}


# ── settings ─────────────────────────────────────────────────────────────────


def _settings(ctx: Any) -> dict[str, Any]:
    def get(key: str, default: Any) -> Any:
        try:
            value = ctx.get_config(key, default=default)
        except Exception:
            return default
        return default if value is None else value

    host_kinds = get("host_kinds", {})
    if not isinstance(host_kinds, dict):
        host_kinds = {}
    return {
        "check_remote": bool(get("check_remote", True)),
        "branches_show_prs": bool(get("branches_show_prs", True)),
        "remote_cache_seconds": _num(get("remote_cache_seconds", gitprobe.DEFAULT_REMOTE_CACHE_SECONDS), 60),
        "remote_timeout_seconds": _num(get("remote_timeout_seconds", gitprobe.DEFAULT_REMOTE_TIMEOUT), 8),
        "file_list_cap": int(_num(get("file_list_cap", gitprobe.DEFAULT_FILE_CAP), 200)),
        "host_kinds": host_kinds,
        "work_dirs": _work_dirs(get("work_dirs", list(gitprobe.DEFAULT_WORK_DIRS))),
        "track_writes": bool(get("track_writes", True)),
        "ledger_retention_days": _num(get("ledger_retention_days", ledger.DEFAULT_RETENTION_DAYS), 90),
        "llm_log": bool(get("llm_log", True)),
        "llm_capture_content": bool(get("llm_capture_content", False)),
        "llm_content_retention_days": _num(get("llm_content_retention_days", llm_log.DEFAULT_CONTENT_RETENTION_DAYS), 7),
    }


def _hermes_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        return load_config_readonly() or {}
    except Exception:
        return {}


def _work_dirs(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [v for v in value.replace(";", ",").split(",")]
    if not isinstance(value, (list, tuple)):
        return gitprobe.DEFAULT_WORK_DIRS
    return tuple(str(v).strip() for v in value if str(v).strip())


def _num(value: Any, default: float) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _session_cwd() -> Path:
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return Path(resolve_agent_cwd())
    except Exception:
        return Path(os.getcwd())


def _resolve_path(raw: str | None) -> Path:
    base = _session_cwd()
    if not raw or not str(raw).strip():
        return base
    p = Path(os.path.expandvars(os.path.expanduser(str(raw).strip().strip('"'))))
    return p if p.is_absolute() else base / p


def _branches(ctx: Any, path: Path, *, fresh: bool = False) -> dict[str, Any]:
    s = _settings(ctx)
    if not s["check_remote"]:
        return {"ok": True, "status": "skipped", "branches": []}
    return gitprobe.remote_branches(
        path, fresh=fresh, timeout=s["remote_timeout_seconds"], cache_seconds=s["remote_cache_seconds"],
        host_kinds=s["host_kinds"], with_prs=s["branches_show_prs"],
    )


def _probe(ctx: Any, path: Path, *, check_remote: bool | None = None, fresh: bool = False, file_cap: int | None = None) -> dict[str, Any]:
    s = _settings(ctx)
    return gitprobe.probe(
        path,
        check_remote_enabled=s["check_remote"] if check_remote is None else bool(check_remote),
        fresh=fresh,
        host_kinds=s["host_kinds"],
        file_cap=file_cap or s["file_list_cap"],
        remote_timeout=s["remote_timeout_seconds"],
        remote_cache_seconds=s["remote_cache_seconds"],
        work_dirs=s["work_dirs"],
    )


# ── tool ─────────────────────────────────────────────────────────────────────


def _make_tool_handler(ctx: Any):
    def repo_status(args: dict, **kwargs: Any) -> str:
        try:
            args = args or {}
            path = _resolve_path(args.get("path"))
            result = _probe(
                ctx,
                path,
                check_remote=args.get("check_remote"),
                fresh=bool(args.get("fresh")),
                file_cap=TOOL_FILE_CAP,
            )
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:  # handlers never raise
            logger.warning("repo_status failed: %s", exc)
            return json.dumps({"ok": False, "error": "internal_error", "message": str(exc)}, ensure_ascii=False)

    return repo_status


# ── F2: ledger of where Hermes wrote ─────────────────────────────────────────

_ledger: ledger.Ledger | None = None


def _get_ledger(ctx: Any) -> ledger.Ledger:
    global _ledger
    if _ledger is None:
        _ledger = ledger.Ledger(ledger.default_ledger_path(), retention_days=_settings(ctx)["ledger_retention_days"])
    return _ledger


TRACKED_TOOLS = {"write_file", "patch", "terminal", "execute_code"} | ledger.SETTINGS_TOOLS


def _make_post_tool_hook(ctx: Any):
    def on_post_tool_call(tool_name: str = "", args: dict | None = None, result: Any = None, task_id: str = "", **kwargs: Any) -> None:
        try:
            if tool_name not in TRACKED_TOOLS and not tool_name.lower().startswith(("mcp", "cronjob")):
                return
            if not _settings(ctx)["track_writes"]:
                return
            rows = ledger.build_rows(
                tool_name,
                args if isinstance(args, dict) else {},
                result,
                cwd=str(_session_cwd()),
                profile=ledger.current_profile(getattr(ctx, "profile_name", None)),
                session_id=kwargs.get("session_id") or None,
                task_id=task_id or None,
                tool_call_id=kwargs.get("tool_call_id") or None,
            )
            if rows:
                _get_ledger(ctx).record(rows)
        except Exception as exc:  # a hook must never disturb the agent
            logger.debug("repo-status ledger hook failed: %s", exc)

    return on_post_tool_call


def _parse_scope(text: str, session_id: str | None) -> tuple[str | None, float | None, str]:
    """``''`` → this session; ``all`` → 24 h; ``7d`` / ``12h`` → all sessions in that window."""
    word = (text or "").strip().lower()
    now = time.time()
    if not word or word in ("session", "this"):
        if session_id:
            return session_id, None, tr("scope.this_session")
        return None, now - 86400, tr("scope.last_24h_unknown_session")
    if word in ("all", "vsetko", "všetko"):
        return None, now - 86400, tr("scope.all_sessions_window", "24 h")
    unit = word[-1:]
    try:
        n = float(word[:-1])
    except ValueError:
        return None, now - 86400, tr("scope.all_sessions_window", "24 h")
    hours = n * 24 if unit == "d" else n
    return None, now - hours * 3600, tr("scope.all_sessions_window", word)


def _touched(ctx: Any, *, session_id: str | None, since: float | None, cwd: str | None, with_status: bool = True) -> dict[str, Any]:
    events = _get_ledger(ctx).query(session_id=session_id, since=since)
    status_cache: dict[str, dict[str, Any]] = {}

    def status_for_root(root: str) -> dict[str, Any] | None:
        if root not in status_cache:
            status_cache[root] = gitprobe.probe(root, check_remote_enabled=False, file_cap=2000, work_dirs=())
        return status_cache[root]

    return ledger.summarize_events(events, cwd=cwd, status_for_root=status_for_root if with_status else None)


# ── F4: LLM provider calls ───────────────────────────────────────────────────

_llm: llm_log.LlmLog | None = None
_llm_warned: list[bool] = []


def _get_llm(ctx: Any) -> llm_log.LlmLog:
    global _llm
    if _llm is None:
        s = _settings(ctx)
        _llm = llm_log.LlmLog(
            llm_log.default_db_path(ledger.default_ledger_path()),
            retention_days=s["ledger_retention_days"],
            content_retention_days=s["llm_content_retention_days"],
        )
    return _llm


def _make_llm_hooks(ctx: Any) -> dict[str, Any]:
    def guarded(fn):
        def hook(**kwargs: Any) -> None:
            try:
                s = _settings(ctx)
                if s["llm_log"]:
                    fn(_get_llm(ctx), kwargs, s["llm_capture_content"])
            except Exception as exc:  # observers must never disturb a provider call
                if not _llm_warned:
                    _llm_warned.append(True)  # once per process, then quiet
                    logger.warning("repo-status llm hook failed: %s", exc, exc_info=True)
                else:
                    logger.debug("repo-status llm hook failed: %s", exc)

        return hook

    profile = lambda: ledger.current_profile(getattr(ctx, "profile_name", None))  # noqa: E731
    return {
        "pre_api_request": guarded(lambda log, kw, cap: log.on_pre(kw, capture=cap)),
        "post_api_request": guarded(lambda log, kw, cap: log.on_post(kw, kind="turn", profile=profile(), capture=cap)),
        "api_request_error": guarded(lambda log, kw, cap: log.on_error(kw, profile=profile(), capture=cap)),
        "pre_auxiliary_call": guarded(lambda log, kw, cap: log.on_pre(kw, capture=cap)),
        "post_auxiliary_call": guarded(lambda log, kw, cap: log.on_post(kw, kind="aux", profile=profile(), capture=cap)),
    }


def _llm_view(ctx: Any, *, since: float | None, session_id: str | None = None) -> dict[str, Any]:
    data = llm_log.summarize_calls(_get_llm(ctx).calls(since=since, session_id=session_id))
    s = _settings(ctx)
    data["capture_content"] = s["llm_capture_content"]
    data["enabled"] = s["llm_log"]
    return data


def _settings_view(ctx: Any, *, since: float | None, force_scan: bool = False) -> dict[str, Any]:
    led = _get_ledger(ctx)
    scan = settings_watch.scan(led, force=force_scan)
    data = settings_watch.summarize_settings(led.query(since=since, settings_only=True, limit=20000))
    data["scan"] = {"skipped": scan["skipped"], "changes": scan["events"], "homes": [h["profile"] for h in scan["homes"]]}
    return data


def _hours_arg(text: str, default_hours: float) -> tuple[float, str]:
    word = (text or "").strip().lower()
    if not word:
        return default_hours, f"{default_hours / 24:g} d" if default_hours >= 24 else f"{default_hours:g} h"
    try:
        n = float(word[:-1])
    except ValueError:
        return default_hours, f"{default_hours / 24:g} d"
    return (n * 24, f"{n:g} d") if word.endswith("d") else (n, f"{n:g} h")


def _make_touched_tool(ctx: Any):
    def hermes_touched_paths(args: dict, **kwargs: Any) -> str:
        try:
            args = args or {}
            scope = str(args.get("scope") or "session")
            hours = args.get("hours")
            sid = kwargs.get("session_id") or None
            if scope == "session" and sid:
                session_id, since = sid, None
            else:
                session_id = None
                since = time.time() - float(hours or 24) * 3600
            data = _touched(ctx, session_id=session_id, since=since, cwd=str(_session_cwd()))
            data["scope"] = "session" if session_id else f"all sessions, last {hours or 24} h"
            return json.dumps(data, ensure_ascii=False, default=str)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    return hermes_touched_paths


TOUCHED_SCHEMA = {
    "name": "hermes_touched_paths",
    "description": (
        "List where Hermes wrote files (recorded by the repo-status plugin from write_file / patch / "
        "MCP file tools; terminal and execute_code are listed as commands with their folder, files "
        "unknown). Grouped by git repository, folders outside the session's working directory first, "
        "with whether each file is still uncommitted. Use it before reporting work as done or before "
        "committing, to find changes made in other folders or worktrees. Read-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": ["session", "all"], "description": "This session (default) or all sessions."},
            "hours": {"type": "number", "description": "Window for scope=all (default 24)."},
        },
        "required": [],
    },
}


# ── slash command ────────────────────────────────────────────────────────────

SLASH_HELP_KEYS = (
    ("**/repo**", "help.title"),
    ("`/repo`", "help.repo"),
    ("`/repo <path>`", "help.path"),
    ("`/repo check [path]`", "help.check"),
    ("`/repo branches [check] [path]`", "help.branches"),
    ("`/repo local [path]`", "help.local"),
    ("`/repo touched` · `touched all` · `touched 7d`", "help.touched"),
    ("`/repo settings [24h|7d|30d]`", "help.settings"),
    ("`/repo llm [session|24h|7d]`", "help.llm"),
    ("`/repo honcho`", "help.honcho"),
    ("`/repo env`", "help.env"),
    ("`/repo disk [refresh]`", "help.disk"),
)


def slash_help() -> str:
    lines = [f"{cmd} – {tr(key)}" for cmd, key in SLASH_HELP_KEYS]
    lines.append(tr("help.full_diff"))
    return "\n".join(lines)


def _make_slash_handler(ctx: Any):
    def handle(raw_args: str = "", **kwargs: Any) -> str:
        try:
            text = (raw_args or "").strip()
            word, _, rest = text.partition(" ")
            fresh = False
            check_remote = None
            if word in ("help", "pomoc", "?"):
                return slash_help()
            if word in ("env", "premenne", "premenné", "vars"):
                data = _env_view("gateway")
                # what the next tool command of THIS chat would receive
                data["chat"] = {"HERMES_SESSION_ID": kwargs.get("session_id") or "", "HERMES_SESSION_PLATFORM": kwargs.get("platform") or "",
                                "TERMINAL_CWD": str(_session_cwd())}
                return render.render_env(data)
            if word in ("disk", "miesto", "space"):
                root = ledger.hermes_root()
                data = disk_usage.overview(root, ledger.default_ledger_path(), root / "state.db", force=rest.strip() == "refresh")
                return render.render_disk(data)
            if word in ("honcho", "memory", "pamat", "pamäť"):
                data = honcho_status.status(ledger.hermes_root(), _hermes_config(), check=True)
                return render.render_honcho(data)
            if word in ("llm", "ai", "provider", "providers"):
                arg = rest.strip().lower()
                sid = kwargs.get("session_id") or None
                if arg in ("session", "chat", "this") and sid:
                    data = _llm_view(ctx, since=None, session_id=sid)
                    return render.render_llm(data, label_text=tr("scope.this_session"))
                hours, window = _hours_arg(arg, 24)
                data = _llm_view(ctx, since=time.time() - hours * 3600)
                return render.render_llm(data, label_text=tr("scope.last_window", window))
            if word in ("settings", "nastavenia", "skills"):
                hours, window = _hours_arg(rest, 168)
                data = _settings_view(ctx, since=time.time() - hours * 3600)
                return render.render_settings(data, label_text=tr("scope.last_window", window))
            if word in ("touched", "zmeny", "hermes"):
                session_id, since, scope_label = _parse_scope(rest, kwargs.get("session_id") or None)
                data = _touched(ctx, session_id=session_id, since=since, cwd=str(_session_cwd()))
                return render.render_touched(data, label_text=scope_label)
            if word in ("branches", "branch", "vetvy"):
                sub, _, path_text = rest.strip().partition(" ")
                fresh_b = sub in ("check", "refresh")
                target = _resolve_path(path_text if fresh_b else rest)
                return render.render_branches(_branches(ctx, target, fresh=fresh_b))
            if word in ("check", "refresh"):
                fresh, text = True, rest
            elif word in ("local", "lokal"):
                check_remote, text = False, rest
            result = _probe(ctx, _resolve_path(text), check_remote=check_remote, fresh=fresh)
            return render.render(result)
        except Exception as exc:
            logger.warning("/repo failed: %s", exc)
            return f"**Repo status:** {tr('error.internal_error')} – {exc}"

    return handle


# ── CLI ──────────────────────────────────────────────────────────────────────


def _setup_cli(subparser: Any) -> None:
    subparser.add_argument("--lang", default=None, help="output language: en, zh, zh-hant, ja, ar, ru, fr, de, es (default: Hermes display language)")
    subparser.add_argument("path", nargs="?", default=None, help="folder or file inside the repository (default: current directory)")
    subparser.add_argument("--json", action="store_true", help="JSON output")
    subparser.add_argument("--diff", nargs="?", const="", default=None, metavar="FILE", help="print the full diff against HEAD (secret-looking files excluded)")
    subparser.add_argument("--no-remote", action="store_true", help="do not check the server")
    subparser.add_argument("--fresh", action="store_true", help="check the server, bypassing the cache")
    subparser.add_argument("--branches", action="store_true", help="all branches on the server (git ls-remote, no fetch) with open pull requests")
    subparser.add_argument("--touched", action="store_true", help="where Hermes wrote files (ledger), all sessions")
    subparser.add_argument("--hours", type=float, default=None, help="time window in hours: --touched default 24, --settings 168, --llm 24")
    subparser.add_argument("--session", default=None, help="a single session ID for --touched / --llm")
    subparser.add_argument("--settings", action="store_true", help="changes to Hermes settings (skills, memory, cron, config, SOUL, plugins)")
    subparser.add_argument("--llm", action="store_true", help="calls to LLM providers")
    subparser.add_argument("--llm-call", type=int, default=None, metavar="ID", help="one LLM call in detail, including content when captured")
    subparser.add_argument("--env", action="store_true", help="Hermes environment variables (HERMES_*, TERMINAL_*) of this process, no secrets")
    subparser.add_argument("--disk", action="store_true", help="disk usage of Hermes and its cache/scratch work folders (rescan, read-only)")
    subparser.add_argument("--honcho", action="store_true", help="Honcho memory status: provider, settings (no secrets), connection, log events")
    subparser.add_argument("--import-history", action="store_true", help="fill the ledger from session history (state.db, read-only)")
    subparser.add_argument("--days", type=float, default=None, help="days of history to import (default: ledger retention, 90)")
    subparser.add_argument("--dry-run", action="store_true", help="with --import-history: only count, write nothing")


def _plain(text: str) -> str:
    return text.replace("**", "").replace("`", "")


def _make_cli_handler(ctx: Any):
    def handle(args: Any) -> int:
        _utf8_stdout()
        from . import i18n

        i18n.set_language(getattr(args, "lang", None))
        raw = getattr(args, "path", None)
        path = Path(raw).expanduser() if raw else Path(os.getcwd())
        if not path.is_absolute():
            path = Path(os.getcwd()) / path
        as_json = getattr(args, "json", False)

        def out(data: Any, text: str) -> None:
            print(json.dumps(data, ensure_ascii=False, indent=2, default=str) if as_json else _plain(text))

        if getattr(args, "env", False):
            data = _env_view(tr("env.process_cli"))
            out(data, render.render_env(data, with_docs=True))
            return 0
        if getattr(args, "disk", False):
            root = ledger.hermes_root()
            print(tr("cli.disk_scanning"), file=sys.stderr)
            data = disk_usage.refresh(root, ledger.default_ledger_path(), root / "state.db")
            out(data, render.render_disk(data, max_scratch=40))
            return 0
        if getattr(args, "honcho", False):
            data = honcho_status.status(ledger.hermes_root(), _hermes_config(), check=True, days=float(getattr(args, "days", None) or 7))
            out(data, render.render_honcho(data))
            return 0
        if getattr(args, "llm_call", None) is not None:
            item = _get_llm(ctx).content(args.llm_call)
            if item is None:
                print(tr("cli.llm_call_missing", args.llm_call), file=sys.stderr)
                return 1
            print(json.dumps(item["call"], ensure_ascii=False, indent=2, default=str))
            if item["request"] is None and item["response"] is None:
                print("\n" + tr("cli.llm_content_missing"), file=sys.stderr)
            else:
                print("\n── REQUEST ──\n" + (item["request"] or tr("cli.not_stored")))
                print("\n── RESPONSE ──\n" + (item["response"] or tr("cli.not_stored")))
            return 0
        if getattr(args, "llm", False):
            sid = getattr(args, "session", None)
            hours = float(getattr(args, "hours", None) or 24)
            data = _llm_view(ctx, since=None if sid else time.time() - hours * 3600, session_id=sid)
            scope = f"session {sid}" if sid else tr("scope.last_window", f"{hours:g} h")
            out(data, render.render_llm(data, label_text=scope))
            return 0
        if getattr(args, "settings", False):
            hours = float(getattr(args, "hours", None) or 168)
            data = _settings_view(ctx, since=time.time() - hours * 3600, force_scan=True)
            out(data, render.render_settings(data, label_text=tr("scope.last_window", f"{hours / 24:g} d"), max_items=200))
            return 0
        if getattr(args, "import_history", False):
            s = _settings(ctx)
            days = getattr(args, "days", None) or s["ledger_retention_days"] or 90
            report = history_import.import_history(
                _get_ledger(ctx),
                days=days,
                dry_run=getattr(args, "dry_run", False),
                progress=lambda msg: print(msg, file=sys.stderr),
            )
            if as_json:
                print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            else:
                print(history_import.render_report(report))
            return 1 if any(p.get("error") for p in report["profiles"]) else 0
        if getattr(args, "touched", False):
            sid = getattr(args, "session", None)
            hours = float(getattr(args, "hours", None) or 24)
            data = _touched(ctx, session_id=sid, since=None if sid else time.time() - hours * 3600, cwd=str(path))
            scope = f"session {sid}" if sid else tr("scope.all_sessions_window", f"{hours:g} h")
            out(data, render.render_touched(data, label_text=scope, max_files=200))
            print(f"\nLedger: {ledger.default_ledger_path()}", file=sys.stderr)
            return 0
        if getattr(args, "branches", False):
            data = _branches(ctx, path, fresh=getattr(args, "fresh", False))
            out(data, render.render_branches(data, max_items=500))
            return 0 if data.get("ok") else 1
        diff = getattr(args, "diff", None)
        if diff is not None:
            res = gitprobe.full_diff(path, file=diff or None)
            if not res.get("ok"):
                print(res.get("message"), file=sys.stderr)
                return 1
            print(res["diff"] or tr("cli.no_diff"))
            if res["truncated"]:
                print("\n" + tr("cli.diff_truncated"), file=sys.stderr)
            print("\n" + tr("cli.excluded_patterns", ", ".join(res["excluded_globs"])), file=sys.stderr)
            return 0
        result = _probe(ctx, path, check_remote=False if getattr(args, "no_remote", False) else None, fresh=getattr(args, "fresh", False))
        out(result, render.render(result, max_files=200))
        return 0 if result.get("ok") else 1

    return handle


def _utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


# ── registration ─────────────────────────────────────────────────────────────


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="repo_status",
        toolset=PLUGIN_ID,
        schema=REPO_STATUS_SCHEMA,
        handler=_make_tool_handler(ctx),
    )
    ctx.register_tool(
        name="hermes_touched_paths",
        toolset=PLUGIN_ID,
        schema=TOUCHED_SCHEMA,
        handler=_make_touched_tool(ctx),
    )
    ctx.register_hook("post_tool_call", _make_post_tool_hook(ctx))
    for hook_name, fn in _make_llm_hooks(ctx).items():
        ctx.register_hook(hook_name, fn)
    ctx.register_command(
        "repo",
        _make_slash_handler(ctx),
        description="Git repo status: remote, git user, branch, server, uncommitted files",
        args_hint="[check|local|help] [cesta]",
    )
    cli_handler = _make_cli_handler(ctx)

    def setup(subparser: Any) -> None:
        _setup_cli(subparser)
        subparser.set_defaults(func=cli_handler)

    ctx.register_cli_command(
        name=PLUGIN_ID,
        help="Stav git repa (read-only): remote, git user, vetva, server, zmeny",
        setup_fn=setup,
        handler_fn=cli_handler,
    )

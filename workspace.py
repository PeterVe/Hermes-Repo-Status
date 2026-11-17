"""F5 — project folder listing, external communication channels, historic provider usage.

Read-only helpers for the Desktop panel (dashboard/plugin_api.py loads this by path):
  list_dir()          one folder level, folders first (the panel expands lazily)
  project_channels()  messaging channels whose Hermes channel prompt names the project
                      (``<platform>.channel_prompts`` in config.yaml — e.g. a Discord channel
                      whose prompt starts "Project: EXAMPLE.COM")
  usage_history()     providers/models per session from Hermes' own usage table in state.db
                      (internal storage: columns are checked first, read-only, never written)
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

MAX_ENTRIES = 400
PLATFORM_LABELS = {
    "discord": "Discord", "telegram": "Telegram", "slack": "Slack", "teams": "Microsoft Teams", "whatsapp": "WhatsApp",
    "signal": "Signal", "matrix": "Matrix", "mattermost": "Mattermost", "google_chat": "Google Chat", "email": "E-mail",
    "irc": "IRC", "webhook": "Webhook", "homeassistant": "Home Assistant", "qqbot": "QQ", "feishu": "Feishu",
}


def list_dir(path: str | os.PathLike[str], *, limit: int = MAX_ENTRIES) -> dict[str, Any]:
    p = Path(path)
    if not p.is_absolute():
        return {"ok": False, "error": "path must be absolute"}
    if not p.is_dir():
        return {"ok": False, "error": "folder does not exist"}
    entries = []
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries.append({"name": e.name, "path": e.path, "dir": is_dir, "size": None if is_dir else st.st_size,
                                "mtime": st.st_mtime, "hidden": e.name.startswith(".")})
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return {"ok": True, "path": str(p), "count": len(entries), "entries": entries[:limit], "truncated": max(0, len(entries) - limit),
            "is_git": (p / ".git").exists()}


def project_channels(config: dict[str, Any], *, name: str | None, project_id: str | None, folders: list[str] | None = None) -> list[dict[str, Any]]:
    """Channels (any platform with ``channel_prompts``) whose prompt names this project."""
    needles = []
    if name:
        # the name must end there: "EXAMPLE" must not match "Project: EXAMPLE.COM"
        end = r"(?![\w-]|\.\w)"
        needles.append(re.compile(r"(?i)\bproject:\s*" + re.escape(name.strip()) + end))
        needles.append(re.compile(r"(?i)\(hermes project[^)]*\b" + re.escape(name.strip()) + end))
    if project_id:
        needles.append(re.compile(re.escape(project_id.strip())))
    for f in folders or []:
        if f:
            needles.append(re.compile(re.escape(f.replace("\\", "/").rstrip("/")), re.I))
    if not needles:
        return []
    out = []
    for platform, section in (config or {}).items():
        if not isinstance(section, dict):
            continue
        prompts = section.get("channel_prompts")
        if not isinstance(prompts, dict):
            continue
        for channel_id, prompt in prompts.items():
            text = str(prompt or "")
            norm = text.replace("\\", "/")
            if any(n.search(norm) for n in needles):
                first = text.strip().split(". ")[0][:160]
                out.append({"platform": platform, "label": PLATFORM_LABELS.get(platform, platform), "channel_id": str(channel_id),
                            "summary": first, "prompt_chars": len(text)})
    return out


REQUIRED_USAGE_COLUMNS = {"session_id", "model", "billing_provider", "api_call_count", "input_tokens", "output_tokens", "last_seen"}


def usage_history(state_db: str | os.PathLike[str], session_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """session_id → providers/models from state.db ``session_model_usage`` (read-only)."""
    ids = [s for s in session_ids if s][:500]
    db = Path(state_db)
    if not ids or not db.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3.0)
    except sqlite3.Error:
        return {}
    try:
        conn.execute("PRAGMA query_only=1")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(session_model_usage)")}
        if not REQUIRED_USAGE_COLUMNS <= cols:
            return {}
        marks = ",".join("?" * len(ids))
        out: dict[str, list[dict[str, Any]]] = {}
        for sid, prov, model, calls, tin, tout, last in conn.execute(
            f"SELECT session_id, billing_provider, model, SUM(api_call_count), SUM(input_tokens), SUM(output_tokens), MAX(last_seen) "
            f"FROM session_model_usage WHERE session_id IN ({marks}) GROUP BY session_id, billing_provider, model", ids
        ):
            out.setdefault(sid, []).append({"provider": prov, "model": model, "calls": calls or 0, "input_tokens": tin or 0,
                                            "output_tokens": tout or 0, "last_ts": last, "source": "usage"})
        return out
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def merge_providers(live: list[dict[str, Any]] | None, history: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Live (F4 log) rows win; usage-table rows fill models the log has not seen."""
    seen = {(p["provider"], p["model"]) for p in live or []}
    merged = list(live or []) + [h for h in history or [] if (h["provider"], h["model"]) not in seen]
    return sorted(merged, key=lambda p: -(p.get("last_ts") or 0))

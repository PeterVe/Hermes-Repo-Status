"""Honcho memory — is it the active memory provider, how is it configured, can we reach it,
and what has it been doing (from Hermes' own log). Read-only.

Secrets: API keys, tokens and passwords from honcho.json are never returned — not even a
suffix. The connection check is an anonymous GET of the server's base URL (no credentials
are sent), so it tells "reachable / TLS / DNS" apart without touching the account.
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SECRET_KEY = re.compile(r"(?i)(key|token|secret|password|passwd|auth|credential|cookie)")
SHOWN_FIELDS = (
    "baseUrl", "environment", "workspace", "peerName", "aiPeer", "sessionStrategy", "recallMode",
    "dialecticDynamic", "writeFrequency", "contextTokens", "dialecticReasoningLevel", "enabled",
)
LOG_TAIL_BYTES = 3 * 1024 * 1024
_LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (\w+) (?:\[[^\]]*\] )?([\w.]+): (.*)$")
_SESSION_KEY = re.compile(r"\bfor ([\w.:-]{6,})")
_URL_CRED = re.compile(r"(https?://)[^/@\s]+@")


def _redact(text: str) -> str:
    text = _URL_CRED.sub(r"\1***@", text)
    return re.sub(r"(?i)((?:api[_-]?key|token|authorization|bearer)\s*[=:]?\s*)\S+", r"\1***", text)


def read_config(hermes_home: Path, config: dict[str, Any], host: str = "hermes") -> dict[str, Any]:
    """Active provider + the Honcho settings Hermes uses (host block over root), secrets dropped."""
    mem = (config or {}).get("memory") or {}
    provider = mem.get("provider") or None
    plugins_cfg = (config or {}).get("plugins") or {}
    enabled = set(plugins_cfg.get("enabled") or [])
    out: dict[str, Any] = {
        "provider": provider,
        "active": provider == "honcho",
        "memory_enabled": mem.get("memory_enabled"),
        "plugin_installed": (hermes_home / "plugins" / "honcho").is_dir(),
        "plugin_enabled": "honcho" in enabled,
        "desktop_plugin_installed": (hermes_home / "plugins" / "hermes-honcho-plugin").is_dir(),
        "desktop_plugin_enabled": "hermes-honcho-plugin" in enabled,
        "config_path": None,
        "settings": {},
        "has_api_key": False,
        "alias_count": 0,
    }
    path = hermes_home / "honcho.json"
    if not path.exists():
        return out
    out["config_path"] = str(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        out["config_error"] = f"honcho.json sa nedá prečítať: {type(exc).__name__}"
        return out
    merged = {k: v for k, v in raw.items() if not isinstance(v, dict)}
    host_block = ((raw.get("hosts") or {}).get(host)) if isinstance(raw.get("hosts"), dict) else None
    if isinstance(host_block, dict):
        merged.update(host_block)
    out["has_api_key"] = any(SECRET_KEY.search(k) and v for k, v in merged.items())
    aliases = merged.get("userPeerAliases")
    out["alias_count"] = len(aliases) if isinstance(aliases, dict) else 0
    out["settings"] = {k: merged[k] for k in SHOWN_FIELDS if k in merged and not SECRET_KEY.search(k)}
    return out


def check_connection(base_url: str | None, *, timeout: float = 5.0) -> dict[str, Any]:
    """Anonymous GET of the base URL: DNS → TCP → TLS → HTTP, first failure wins."""
    if not base_url:
        return {"ok": False, "stage": "config", "error": "baseUrl nie je nastavená"}
    started = time.time()
    req = urllib.request.Request(base_url.rstrip("/") + "/", method="GET", headers={"User-Agent": "repo-status-plugin"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
            status = resp.status
        return {"ok": True, "stage": "http", "status": status, "ms": int((time.time() - started) * 1000)}
    except urllib.error.HTTPError as exc:  # the server answered: reachable, TLS fine
        return {"ok": True, "stage": "http", "status": exc.code, "ms": int((time.time() - started) * 1000)}
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            stage, msg = "tls", f"TLS certifikát neplatný: {reason.verify_message or reason}"
        elif isinstance(reason, ssl.SSLError):
            stage, msg = "tls", f"TLS chyba: {reason}"
        elif isinstance(reason, socket.gaierror):
            stage, msg = "dns", f"DNS: názov servera sa nedá preložiť ({reason})"
        elif isinstance(reason, (TimeoutError, socket.timeout)):
            stage, msg = "timeout", f"server neodpovedal do {timeout:g} s"
        elif isinstance(reason, ConnectionRefusedError):
            stage, msg = "tcp", "spojenie odmietnuté (server nebeží alebo zlý port)"
        else:
            stage, msg = "network", str(reason)
        return {"ok": False, "stage": stage, "error": _redact(msg)[:300], "ms": int((time.time() - started) * 1000)}
    except (TimeoutError, socket.timeout):
        return {"ok": False, "stage": "timeout", "error": f"server neodpovedal do {timeout:g} s"}
    except Exception as exc:  # never break the panel
        return {"ok": False, "stage": "error", "error": _redact(f"{type(exc).__name__}: {exc}")[:300]}


def log_events(log_files: list[Path], *, since: float | None = None, limit: int = 40) -> dict[str, Any]:
    """Honcho lines from Hermes' logs (newest first): level counts, session keys, recent events."""
    events: list[dict[str, Any]] = []
    for path in log_files:
        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                if size > LOG_TAIL_BYTES:
                    fh.seek(size - LOG_TAIL_BYTES)
                text = fh.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if "honcho" not in line.lower():
                continue
            m = _LOG_LINE.match(line)
            if not m:
                continue
            ts_text, level, logger_name, msg = m.groups()
            if "honcho" not in logger_name.lower() and "honcho" not in msg.lower():
                continue
            try:
                ts = time.mktime(time.strptime(ts_text, "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                continue
            if since and ts < since:
                continue
            events.append({"ts": ts, "level": level.upper(), "logger": logger_name, "message": _redact(msg)[:300]})
    seen: set[tuple[float, str]] = set()
    unique = []
    for e in events:  # errors.log repeats agent.log's warnings
        key = (e["ts"], e["message"])
        if key not in seen:
            seen.add(key)
            unique.append(e)
    events = sorted(unique, key=lambda e: -e["ts"])
    counts: dict[str, int] = {}
    session_keys: dict[str, float] = {}
    for e in events:
        counts[e["level"]] = counts.get(e["level"], 0) + 1
        k = _SESSION_KEY.search(e["message"])
        if k and "honcho" in e["logger"].lower():
            session_keys.setdefault(k.group(1).rstrip(".,)"), e["ts"])
    problems = [e for e in events if e["level"] in ("WARNING", "ERROR", "CRITICAL")]
    return {
        "counts": counts,
        "session_keys": [{"key": k, "last_ts": t} for k, t in sorted(session_keys.items(), key=lambda kv: -kv[1])][:20],
        "problems": problems[:15],
        "recent": events[:limit],
    }


def status(hermes_home: Path, config: dict[str, Any], *, check: bool = False, days: float = 7) -> dict[str, Any]:
    cfg = read_config(hermes_home, config)
    out: dict[str, Any] = {"config": cfg, "available": cfg["active"] or cfg["plugin_installed"]}
    if check and cfg["settings"].get("baseUrl"):
        out["connection"] = check_connection(str(cfg["settings"]["baseUrl"]))
    logs = hermes_home / "logs"
    files = [logs / "agent.log", logs / "errors.log"]
    out["logs"] = log_events([f for f in files if f.exists()], since=time.time() - days * 86400)
    return out

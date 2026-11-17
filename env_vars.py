"""Hermes environment variables as the running process sees them — read-only, no secrets.

Shows the core HERMES_* / TERMINAL_* variables (set or not, with Hermes' own description from
``docs/reference/environment-variables.md``) and every other HERMES_* / TERMINAL_* variable
that is set. Anything whose NAME looks secret (key, token, secret, password, auth, credential,
cookie, client id/secret) is never returned — only counted — and values that look like
credentials are masked as a second line of defence.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

PREFIXES = ("HERMES_", "TERMINAL_")
SECRET_NAME = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSW|AUTH|CREDENTIAL|COOKIE|CLIENT_ID|SESSION_TOKEN|PRIVATE)")
SECRET_VALUE = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{20,}\.)")

# (name, group, default shown when unset)
CORE: list[tuple[str, str, str | None]] = [
    ("HERMES_HOME", "základ", None),
    ("HERMES_PROFILE", "základ", "default"),
    ("TERMINAL_CWD", "základ", "terminal.cwd v config.yaml / adresár spustenia"),
    ("TERMINAL_ENV", "základ", "local"),
    ("HERMES_SESSION_ID", "session", "nastavuje sa pre každý príkaz nástroja"),
    ("HERMES_SESSION_PLATFORM", "session", "nastavuje sa pre každý príkaz nástroja"),
    ("HERMES_SESSION_KEY", "session", "nastavuje sa pre každý príkaz nástroja"),
    ("HERMES_SESSION_SOURCE", "session", "nastavuje sa pre každý príkaz nástroja"),
    ("HERMES_KANBAN_TASK", "kanban", "len vo worker procese"),
    ("HERMES_KANBAN_BOARD", "kanban", "len vo worker procese"),
    ("HERMES_MAX_ITERATIONS", "správanie", "500"),
    ("HERMES_REDACT_SECRETS", "správanie", "true"),
    ("HERMES_YOLO_MODE", "správanie", "vypnuté"),
    ("HERMES_SAFE_MODE", "správanie", "vypnuté"),
    ("HERMES_ACCEPT_HOOKS", "správanie", "vypnuté"),
    ("HERMES_INFERENCE_PROVIDER", "správanie", "podľa config.yaml"),
    ("HERMES_ENABLE_PROJECT_PLUGINS", "správanie", "vypnuté"),
    ("HERMES_PLUGIN_PAYLOAD_MAX_CHARS", "správanie", "50000"),
]
_DOC_ROW = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]+)`\s*\|\s*(.+?)\s*\|\s*$")


def load_docs(hermes_home: Path) -> dict[str, str]:
    path = hermes_home / "hermes-agent" / "website" / "docs" / "reference" / "environment-variables.md"
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        m = _DOC_ROW.match(line)
        if m and m.group(1) not in out:
            desc = re.sub(r"`([^`]*)`", r"\1", m.group(2))
            desc = re.sub(r"\*\*([^*]*)\*\*", r"\1", desc)
            out[m.group(1)] = desc[:400]
    return out


def is_secret_name(name: str) -> bool:
    return bool(SECRET_NAME.search(name))


def _safe_value(value: str) -> str:
    if SECRET_VALUE.search(value):
        return "*** (vyzerá ako prihlasovací údaj)"
    return value if len(value) <= 400 else value[:400] + "…"


def snapshot(environ: Mapping[str, str], hermes_home: Path, *, docs: dict[str, str] | None = None,
             resolved: Mapping[str, str] | None = None) -> dict[str, Any]:
    """``resolved`` = values Hermes computes when the variable is unset (e.g. HERMES_HOME)."""
    docs = docs if docs is not None else load_docs(hermes_home)
    resolved = resolved or {}
    core_names = {n for n, _g, _d in CORE}
    core = []
    for name, group, default in CORE:
        raw = environ.get(name)
        item = {"name": name, "group": group, "set": raw is not None, "doc": docs.get(name)}
        if raw is not None:
            item["value"] = _safe_value(raw)
        elif name in resolved:
            item["value"], item["resolved"] = resolved[name], True
        else:
            item["default"] = default
        core.append(item)
    other = []
    hidden = 0
    for name in sorted(environ):
        if not name.startswith(PREFIXES) or name in core_names:
            continue
        if is_secret_name(name):
            hidden += 1
            continue
        other.append({"name": name, "value": _safe_value(environ[name]), "doc": docs.get(name)})
    return {"core": core, "other": other, "hidden_secret_count": hidden, "documented": len(docs), "pid": os.getpid()}

"""Plugin strings in the languages Hermes supports (en, zh, zh-hant, ja, ar, ru, fr, de, es).

One flat catalog per language in ``locales/<lang>.json`` is shared by the chat/CLI output
(this module) and the Desktop panel (served by ``GET /i18n``). Placeholders are positional
(``{0}``, ``{1}``) — the same form the Desktop's plugin translator uses. A missing key falls
back to English, then to the key itself.

The active language follows Hermes (``display.language`` / ``HERMES_LANGUAGE`` via
``agent.i18n.get_language``); outside Hermes (tests) it is English unless set explicitly.
Self-contained: no relative imports (dashboard/plugin_api.py loads it by path).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

LANGUAGES = ("en", "zh", "zh-hant", "ja", "ar", "ru", "fr", "de", "es")
DEFAULT = "en"
LOCALES_DIR = Path(__file__).resolve().parent / "locales"

_cache: dict[str, dict[str, str]] = {}
_lock = threading.Lock()
_forced: list[str] = []  # tests / explicit override


def catalog(lang: str) -> dict[str, str]:
    lang = normalize(lang)
    with _lock:
        hit = _cache.get(lang)
    if hit is not None:
        return hit
    try:
        data = json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))
        data = {k: v for k, v in data.items() if isinstance(v, str) and not k.startswith("_")}
    except (OSError, ValueError):
        data = {}
    with _lock:
        _cache[lang] = data
    return data


def normalize(lang: Any) -> str:
    text = str(lang or "").strip().lower().replace("_", "-")
    if text in LANGUAGES:
        return text
    if text in ("zh-tw", "zh-hk", "zh-mo"):
        return "zh-hant"
    base = text.split("-")[0]
    return base if base in LANGUAGES else DEFAULT


def current_language() -> str:
    if _forced:
        return _forced[-1]
    try:
        from agent.i18n import get_language

        return normalize(get_language())
    except Exception:
        return DEFAULT


def set_language(lang: str | None) -> None:
    """Force a language (tests, CLI flag); ``None`` returns to following Hermes."""
    _forced.clear()
    if lang:
        _forced.append(normalize(lang))


def tr(key: str, *args: Any, lang: str | None = None) -> str:
    target = normalize(lang) if lang else current_language()
    value = catalog(target).get(key)
    if value is None and target != DEFAULT:
        value = catalog(DEFAULT).get(key)
    if value is None:
        value = key
    if not args:
        return value
    try:
        return value.format(*args)
    except (IndexError, KeyError, ValueError):
        return value


def all_catalogs() -> dict[str, dict[str, str]]:
    return {lang: catalog(lang) for lang in LANGUAGES}

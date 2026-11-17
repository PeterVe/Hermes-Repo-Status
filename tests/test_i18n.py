"""Language catalogs: complete English, consistent translations, inline copy in plugin.js in sync."""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import i18n  # noqa: E402
import render  # noqa: E402
import sync_locales  # noqa: E402

PLACEHOLDER = re.compile(r"\{(\d+)\}")
LABEL_PREFIXES = {
    # prefix used with label()/L() → codes the code can produce
    "remote": ["up_to_date", "local_ahead", "behind", "diverged", "remote_newer", "not_on_remote", "no_upstream", "detached", "no_remote", "skipped", "error"],
    "remote_short": ["up_to_date", "local_ahead", "behind", "diverged", "remote_newer", "not_on_remote", "no_upstream", "detached", "no_remote", "skipped", "error"],
    "reason": ["uncommitted", "unpushed", "server_newer", "not_on_remote", "no_upstream", "remote_unreachable", "conflicts"],
    "error": ["not_a_repo", "bare_repo", "git_error", "internal_error"],
    "category": ["skill", "memory", "cron", "config", "agent", "plugin", "hook"],
    "env.group": ["base", "session", "kanban", "behavior"],
    "gitscope": ["local", "global", "system"],
    "honcho.stage": ["config", "dns", "tcp", "tls", "timeout", "network", "error"],
}


def used_keys() -> set[str]:
    keys: set[str] = set()
    for path in [*ROOT.glob("*.py"), ROOT / "desktop" / "plugin.js"]:
        text = path.read_text(encoding="utf-8")
        keys |= set(re.findall(r"""\b(?:tr|T)\(\s*["']([a-z_][a-z0-9_.]*)["']""", text))
        keys |= set(re.findall(r"""_head\(\s*["']([a-z_][a-z0-9_.]*)["']""", text))
        keys |= set(re.findall(r"""\(["'][^"']*["'],\s*["'](help\.[a-z_]+)["']\)""", text))
    keys = {k for k in keys if not k.endswith(".")}  # tr("changes." + key) builds the key at runtime
    keys |= {f"changes.{k}" for k in ("staged", "unstaged", "untracked", "conflicted")}
    for prefix, codes in LABEL_PREFIXES.items():
        keys |= {f"{prefix}.{c}" for c in codes}
    return keys


class CatalogTests(unittest.TestCase):
    def setUp(self):
        i18n._cache.clear()
        i18n.set_language(None)

    def tearDown(self):
        i18n.set_language(None)

    def test_english_has_every_used_key(self):
        en = i18n.catalog("en")
        missing = sorted(k for k in used_keys() if k not in en)
        self.assertEqual(missing, [], f"keys missing in locales/en.json: {missing}")

    def test_all_languages_present_and_consistent(self):
        en = i18n.catalog("en")
        for lang in i18n.LANGUAGES:
            with self.subTest(lang=lang):
                path = ROOT / "locales" / f"{lang}.json"
                self.assertTrue(path.exists(), f"missing {path.name}")
                cat = json.loads(path.read_text(encoding="utf-8"))
                extra = sorted(k for k in cat if not k.startswith("_") and k not in en)
                self.assertEqual(extra, [], f"{lang}: keys not in English: {extra}")
                missing = sorted(k for k in en if k not in cat)
                self.assertEqual(missing, [], f"{lang}: untranslated keys: {missing}")
                for k, v in cat.items():
                    if k.startswith("_"):
                        continue
                    self.assertIsInstance(v, str, f"{lang}:{k}")
                    self.assertEqual(sorted(set(PLACEHOLDER.findall(v))), sorted(set(PLACEHOLDER.findall(en[k]))), f"{lang}:{k} placeholders differ")

    def test_inline_english_in_plugin_js_is_in_sync(self):
        js = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
        self.assertEqual(sync_locales.current_block(js), sync_locales.en_block(),
                         "desktop/plugin.js inline EN is stale - run: python scripts/sync_locales.py")

    def test_fallback_and_normalize(self):
        self.assertEqual(i18n.normalize("de-AT"), "de")
        self.assertEqual(i18n.normalize("zh_TW"), "zh-hant")
        self.assertEqual(i18n.normalize("sk"), "en")
        self.assertEqual(i18n.tr("no.such.key"), "no.such.key")
        self.assertEqual(i18n.tr("common.and_more", 5, lang="en"), "and 5 more")
        i18n._cache["de"] = {}  # an empty catalog falls back to English per key
        self.assertEqual(i18n.tr("ui.files", lang="de"), "Files")
        i18n._cache.clear()

    def test_render_follows_language(self):
        p = {"ok": False, "error": "not_a_repo", "path": "C:/x"}
        en = render.render(p)
        i18n.set_language("de")
        de = render.render(p)
        self.assertIn("not inside a git repository", en)
        self.assertNotEqual(en, de)


if __name__ == "__main__":
    unittest.main()

"""F3: Hermes settings changes — tool events, file classification, fingerprint watch."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ledger  # noqa: E402
import render  # noqa: E402
import settings_watch  # noqa: E402


def touch(p: Path, text: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class ToolEventTests(unittest.TestCase):
    def test_skill_manage_operations(self):
        args = {"operations": [{"action": "patch", "name": "hermes-agent", "file_path": "references/x.md", "old_string": "SECRET", "new_string": "S2"}, {"action": "view", "name": "a"}]}
        ev = ledger.extract_events("skill_manage", args, json.dumps({"success": True}))
        self.assertEqual(len(ev), 1)  # view is read-only
        self.assertEqual((ev[0]["category"], ev[0]["name"], ev[0]["detail"]), ("skill", "hermes-agent", "hermes-agent/references/x.md"))
        self.assertNotIn("SECRET", json.dumps(ev))

    def test_skill_manage_flat_and_failed(self):
        self.assertEqual(ledger.extract_events("skill_manage", {"action": "create", "name": "n"}, "{}")[0]["action"], "create")
        self.assertEqual(ledger.extract_events("skill_manage", {"action": "create", "name": "n"}, json.dumps({"error": "x"})), [])

    def test_memory_never_stores_content(self):
        args = {"target": "user", "operations": [{"action": "add", "content": "Alice likes tea"}]}
        ev = ledger.extract_events("memory", args, "{}")
        self.assertEqual((ev[0]["category"], ev[0]["name"], ev[0]["action"]), ("memory", "user", "add"))
        self.assertNotIn("tea", json.dumps(ev))

    def test_cronjob(self):
        ev = ledger.extract_events("cronjob_manage", {"action": "create", "name": "daily", "schedule": "0 7 * * *", "prompt": "P"}, "{}")
        self.assertEqual((ev[0]["category"], ev[0]["name"]), ("cron", "daily"))
        self.assertEqual(ledger.extract_events("cronjob_manage", {"action": "list"}, "{}"), [])

    def test_settings_rows_have_no_folder(self):
        rows = ledger.build_rows("memory", {"target": "memory", "action": "add"}, "{}", cwd="C:/work", profile="default", session_id="s", task_id=None)
        self.assertIsNone(rows[0]["path"])
        self.assertIsNone(rows[0]["git_root"])
        self.assertEqual(rows[0]["category"], "memory")


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "hermes"
        touch(self.home / "skills" / "devops" / "deployer" / "SKILL.md")
        touch(self.home / "skills" / "devops" / "deployer" / "references" / "a.md")

    def tearDown(self):
        self.tmp.cleanup()

    def test_categories(self):
        h = [self.home]
        cases = {
            "config.yaml": ("config", "config.yaml"),
            "SOUL.md": ("agent", "SOUL.md"),
            "memories/USER.md": ("memory", "USER.md"),
            "cron/jobs.json": ("cron", "jobs.json"),
            "plugins/repo-status/plugin.yaml": ("plugin", "repo-status"),
            "skills/devops/deployer/references/a.md": ("skill", "deployer"),
        }
        for rel, expected in cases.items():
            with self.subTest(rel=rel):
                self.assertEqual(ledger.settings_category(str(self.home / rel), h), expected)
        self.assertIsNone(ledger.settings_category(str(self.home / "cache" / "x.py"), h))
        self.assertIsNone(ledger.settings_category(str(Path(self.tmp.name) / "elsewhere.txt"), h))

    def test_write_file_into_skills_is_a_settings_row(self):
        p = self.home / "skills" / "devops" / "deployer" / "SKILL.md"
        orig = ledger.hermes_homes
        ledger.hermes_homes = lambda root=None: [self.home]
        try:
            row = ledger.build_rows("write_file", {"path": str(p)}, "{}", cwd=None, profile="default", session_id="s", task_id=None)[0]
        finally:
            ledger.hermes_homes = orig
        self.assertEqual((row["category"], row["item"]), ("skill", "deployer"))


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "hermes"
        touch(self.home / "config.yaml", "a: 1")
        touch(self.home / "SOUL.md")
        touch(self.home / ".env", "SECRET=1")
        touch(self.home / "auth.json", "{}")
        touch(self.home / "memories" / "MEMORY.md")
        touch(self.home / "memories" / "MEMORY.md.lock")
        touch(self.home / "skills" / "s1" / "SKILL.md")
        touch(self.home / "skills" / "s1" / "node_modules" / "x.js")
        touch(self.home / "cache" / "junk.py")
        self.led = ledger.Ledger(base / "ledger.db")
        settings_watch._last_scan.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self):
        return settings_watch.scan(self.led, homes=[self.home], force=True)

    def test_watched_set_excludes_secrets_and_junk(self):
        rels = set(settings_watch.fingerprint(self.home))
        self.assertEqual(rels, {"config.yaml", "SOUL.md", "memories/MEMORY.md", "skills/s1/SKILL.md"})

    def test_baseline_then_changes(self):
        first = self.scan()
        self.assertEqual(first["events"], 0)  # baseline only
        touch(self.home / "config.yaml", "a: 22")
        touch(self.home / "skills" / "s2" / "SKILL.md")
        (self.home / "SOUL.md").unlink()
        touch(self.home / ".env", "SECRET=2")  # never reported
        second = self.scan()
        self.assertEqual(second["events"], 3)
        rows = {(r["category"], r["item"], r["action"]) for r in self.led.query(settings_only=True)}
        self.assertEqual(rows, {("config", "config.yaml", "modify"), ("skill", "s2", "create"), ("agent", "SOUL.md", "delete")})
        self.assertEqual(self.scan()["events"], 0)  # nothing new

    def test_throttle(self):
        settings_watch.scan(self.led, homes=[self.home])
        self.assertTrue(settings_watch.scan(self.led, homes=[self.home])["skipped"])

    def test_summary_attribution(self):
        now = time.time()
        self.led.record(ledger.build_rows("skill_manage", {"action": "patch", "name": "s1"}, "{}", cwd=None, profile="default", session_id="sess", task_id=None, now=now - 60))
        self.scan()
        touch(self.home / "skills" / "s1" / "SKILL.md", "changed by Hermes")
        os.utime(self.home / "skills" / "s1" / "SKILL.md", (now + 5, now + 5))
        touch(self.home / "memories" / "MEMORY.md", "changed by hand!")
        self.scan()
        data = settings_watch.summarize_settings(self.led.query(settings_only=True))
        cats = {c["category"]: c for c in data["categories"]}
        s1 = cats["skill"]["items"][0]
        self.assertEqual(s1["item"], "s1")
        self.assertTrue(s1["by_hermes"])
        self.assertFalse(s1["unattributed"])
        mem = cats["memory"]["items"][0]
        self.assertTrue(mem["unattributed"])
        text = render.render_settings(data, label="test")
        self.assertIn("Skills", text)
        self.assertIn("pôvod neznámy", text)

    def test_touched_view_ignores_settings_tool_rows(self):
        self.led.record(ledger.build_rows("memory", {"action": "add"}, "{}", cwd=str(self.home), profile="d", session_id="s", task_id=None))
        data = ledger.summarize_events(self.led.query(), cwd=str(self.home))
        self.assertEqual(data["groups"], [])


if __name__ == "__main__":
    unittest.main()

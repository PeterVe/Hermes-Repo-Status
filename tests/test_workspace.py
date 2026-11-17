"""F5: folder listing, project channels, session capacity and providers."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import llm_log  # noqa: E402
import workspace  # noqa: E402
try:  # discover (tests/ on sys.path) or `-m unittest tests.test_workspace`
    from test_llm_log import post  # noqa: E402
except ImportError:  # pragma: no cover
    from tests.test_llm_log import post  # noqa: E402


class ListDirTests(unittest.TestCase):
    def test_folders_first_and_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "b.txt").write_text("xx")
            (base / "a_dir").mkdir()
            (base / ".git").mkdir()
            d = workspace.list_dir(base)
            self.assertTrue(d["ok"])
            self.assertTrue(d["is_git"])
            self.assertEqual([e["name"] for e in d["entries"]], [".git", "a_dir", "b.txt"])
            self.assertEqual(d["entries"][2]["size"], 2)
            self.assertTrue(d["entries"][0]["hidden"])

    def test_errors_and_limit(self):
        self.assertFalse(workspace.list_dir("relative/path")["ok"])
        self.assertFalse(workspace.list_dir(Path(tempfile.gettempdir()) / "definitely-missing-xyz")["ok"])
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(5):
                (Path(tmp) / f"f{i}").write_text("")
            d = workspace.list_dir(tmp, limit=2)
            self.assertEqual((len(d["entries"]), d["truncated"]), (2, 3))


class ChannelTests(unittest.TestCase):
    CONFIG = {
        "discord": {"channel_prompts": {
            "111": "Project: EXAMPLE.COM. This channel and its threads are for the local repository C:/REPOS/example.com.",
            "222": "Project: planning-demo (Hermes project p_12345678). Planning only.",
            "333": "General chat.",
        }},
        "telegram": {"channel_prompts": {"-100": "Work on C:\\REPOS\\demo.org please"}},
        "model": "x",
    }

    def test_match_by_name_id_and_folder(self):
        by_name = workspace.project_channels(self.CONFIG, name="EXAMPLE.COM", project_id=None)
        self.assertEqual([c["channel_id"] for c in by_name], ["111"])
        self.assertEqual(by_name[0]["label"], "Discord")
        self.assertEqual(by_name[0]["summary"], "Project: EXAMPLE.COM")
        by_id = workspace.project_channels(self.CONFIG, name="other", project_id="p_12345678")
        self.assertEqual([c["channel_id"] for c in by_id], ["222"])
        by_folder = workspace.project_channels(self.CONFIG, name=None, project_id=None, folders=["C:\\REPOS\\demo.org"])
        self.assertEqual([(c["platform"], c["channel_id"]) for c in by_folder], [("telegram", "-100")])

    def test_no_false_prefix_match(self):
        self.assertEqual(workspace.project_channels(self.CONFIG, name="EXAMPLE", project_id=None), [])
        self.assertEqual(workspace.project_channels(self.CONFIG, name=None, project_id=None), [])


class SessionMetaTests(unittest.TestCase):
    def test_capacity_and_providers(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = llm_log.LlmLog(Path(tmp) / "llm.db")
            now = time.time()
            log.on_post(post("a", started_at=now - 100, context_length=272000), kind="turn", profile="d", capture=False)
            log.on_post(post("b", started_at=now - 10, context_length=272000,
                             usage={"input_tokens": 2000, "cache_read_tokens": 50000, "cache_write_tokens": 0, "output_tokens": 10}),
                        kind="turn", profile="d", capture=False)
            log.on_post(post("c", provider="anthropic", model="claude"), kind="aux", profile="d", capture=False)
            meta = log.session_meta(["s1", "missing"])
            self.assertEqual(meta["s1"]["context"]["used"], 52000)  # latest main-turn call
            self.assertEqual(meta["s1"]["context"]["max"], 272000)
            provs = {(p["provider"], p["model"]): p["calls"] for p in meta["s1"]["providers"]}
            self.assertEqual(provs, {("openai-codex", "gpt-x"): 2, ("anthropic", "claude"): 1})
            self.assertNotIn("missing", meta)

    def test_v1_llm_db_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "llm.db"
            schema_v1 = llm_log._SCHEMA.replace(",\n  context_length INTEGER,\n  prompt_tokens INTEGER", "")
            conn = sqlite3.connect(db)
            conn.executescript(schema_v1)
            conn.close()
            log = llm_log.LlmLog(db)
            log.on_post(post("m", context_length=1000), kind="turn", profile="d", capture=False)
            self.assertEqual(log.calls()[0]["context_length"], 1000)

    def test_usage_history_and_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE session_model_usage (session_id TEXT, model TEXT, billing_provider TEXT, api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, last_seen REAL)")
            conn.executemany("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?)", [
                ("s1", "gpt-x", "openai-codex", 5, 100, 10, 1.0),
                ("s1", "gemini", "antigravity-oauth", 2, 50, 5, 2.0),
            ])
            conn.commit()
            conn.close()
            hist = workspace.usage_history(db, ["s1", "s2"])
            self.assertEqual(len(hist["s1"]), 2)
            live = [{"provider": "openai-codex", "model": "gpt-x", "calls": 9, "last_ts": 5.0, "source": "llm_log"}]
            merged = workspace.merge_providers(live, hist["s1"])
            self.assertEqual([(p["provider"], p["source"]) for p in merged], [("openai-codex", "llm_log"), ("antigravity-oauth", "usage")])

    def test_usage_history_schema_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE session_model_usage (session_id TEXT)")
            conn.commit()
            conn.close()
            self.assertEqual(workspace.usage_history(db, ["s1"]), {})
            self.assertEqual(workspace.usage_history(Path(tmp) / "none.db", ["s1"]), {})


if __name__ == "__main__":
    unittest.main()

"""F2b: history import from a synthetic state.db with the columns Hermes uses."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import history_import  # noqa: E402
import ledger  # noqa: E402


def make_state_db(path: Path, *, version: int = 32, drop_column: str | None = None) -> sqlite3.Connection:
    msg_cols = ["id INTEGER PRIMARY KEY", "session_id TEXT", "role TEXT", "content TEXT", "tool_call_id TEXT", "tool_calls TEXT", "tool_name TEXT", "timestamp REAL"]
    if drop_column:
        msg_cols = [c for c in msg_cols if not c.startswith(drop_column + " ")]
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER)")
    conn.execute("INSERT INTO schema_version VALUES (?)", (version,))
    conn.execute(f"CREATE TABLE messages ({', '.join(msg_cols)})")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, profile_name TEXT)")
    conn.commit()
    return conn


def call(cid: str, name: str, args: dict) -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class HistoryImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.work = self.base / "work"
        (self.work / ".git").mkdir(parents=True)
        self.db_path = self.base / "state.db"
        conn = make_state_db(self.db_path)
        now = time.time()
        conn.execute("INSERT INTO sessions VALUES ('s1', ?, 'default')", (str(self.work),))
        rows = [
            ("s1", "assistant", None, None, json.dumps([call("c1", "write_file", {"path": "a.txt", "content": "SECRET"})]), None, now - 100),
            ("s1", "tool", json.dumps({"bytes_written": 6, "resolved_path": str(self.work / "a.txt")}), "c1", None, "write_file", now - 99),
            ("s1", "assistant", None, None, json.dumps([call("c2", "patch", {"path": "b.py", "old_string": "x", "new_string": "y"}), call("c3", "terminal", {"command": "npm install"})]), None, now - 90),
            ("s1", "tool", json.dumps({"success": True, "files_modified": [str(self.work / "b.py")]}), "c2", None, "patch", now - 89),
            ("s1", "tool", "ok", "c3", None, "terminal", now - 88),
            ("s1", "assistant", None, None, json.dumps([call("c4", "write_file", {"path": "denied.txt", "content": "x"})]), None, now - 80),
            ("s1", "tool", json.dumps({"error": "Refusing"}), "c4", None, "write_file", now - 79),
            ("s1", "assistant", None, None, json.dumps([call("c5", "read_file", {"path": "a.txt"})]), None, now - 70),
            ("s1", "assistant", None, None, json.dumps([call("c6", "write_file", {"path": "old.txt", "content": "x"})]), None, now - 200 * 86400),
            ("s1", "tool", json.dumps({"bytes_written": 1}), "c6", None, "write_file", now - 200 * 86400),
        ]
        conn.executemany("INSERT INTO messages(session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp) VALUES (?,?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
        ledger._root_cache.clear()
        self.target = ledger.Ledger(self.base / "ledger.db")

    def tearDown(self):
        self.tmp.cleanup()

    def run_import(self, **kw):
        return history_import.import_history(self.target, dbs=[("default", self.db_path)], **kw)

    def test_import_rows_and_evidence(self):
        report = self.run_import(days=90)
        p = report["profiles"][0]
        self.assertNotIn("error", p)
        self.assertEqual(p["calls"], 4)  # c1 c2 c3 c4 (read_file ignored, c6 too old)
        self.assertEqual(p["failed"], 1)  # c4 refused
        rows = {r["tool_call_id"]: r for r in self.target.query()}
        self.assertEqual(set(rows), {"c1", "c2", "c3"})
        self.assertEqual(rows["c1"]["source"], "history")
        self.assertEqual(rows["c1"]["evidence"], "documented")
        self.assertEqual(rows["c1"]["path_guessed"], 0)  # resolved_path from the result
        self.assertEqual(Path(rows["c1"]["git_root"]), self.work)
        self.assertEqual(rows["c3"]["evidence"], "probable")
        self.assertEqual(Path(rows["c3"]["path"]), self.work)  # session cwd
        self.assertEqual(rows["c1"]["profile"], "default")

    def test_rerun_is_idempotent(self):
        self.run_import(days=90)
        again = self.run_import(days=90)
        self.assertEqual(again["total_rows"], 0)
        self.assertEqual(again["profiles"][0]["skipped_known"], 3)
        self.assertEqual(len(self.target.query()), 3)

    def test_hook_rows_are_not_duplicated(self):
        self.target.record(ledger.build_rows("write_file", {"path": "a.txt"}, "{}", cwd=str(self.work), profile="default", session_id="s1", task_id=None, tool_call_id="c1"))
        report = self.run_import(days=90)
        self.assertEqual(report["profiles"][0]["skipped_known"], 1)
        self.assertEqual(len([r for r in self.target.query() if r["tool_call_id"] == "c1"]), 1)

    def test_dry_run_writes_nothing(self):
        report = self.run_import(days=90, dry_run=True)
        self.assertEqual(report["total_rows"], 3)
        self.assertEqual(self.target.query(), [])

    def test_all_days(self):
        report = self.run_import(days=0)
        self.assertEqual(report["profiles"][0]["calls"], 5)

    def test_source_db_is_not_modified(self):
        before = self.db_path.read_bytes()
        self.run_import(days=90)
        self.assertEqual(before, self.db_path.read_bytes())

    def test_schema_mismatch_reports_error(self):
        bad = self.base / "bad.db"
        make_state_db(bad, drop_column="tool_calls").close()
        report = history_import.import_history(self.target, dbs=[("default", bad)])
        self.assertIn("tool_calls", report["profiles"][0]["error"])
        self.assertIn("❌", history_import.render_report(report))

    def test_untested_version_warns(self):
        other = self.base / "v99.db"
        make_state_db(other, version=99).close()
        report = history_import.import_history(self.target, dbs=[("default", other)])
        self.assertFalse(report["profiles"][0]["schema_tested"])
        self.assertIn("nebola s pluginom testovaná", history_import.render_report(report))

    def test_find_state_dbs(self):
        root = self.base / "home"
        (root / "profiles" / "work").mkdir(parents=True)
        (root / "state.db").write_bytes(b"")
        (root / "profiles" / "work" / "state.db").write_bytes(b"")
        self.assertEqual([p for p, _ in history_import.find_state_dbs(root)], ["default", "work"])


class LedgerMigrationTests(unittest.TestCase):
    def test_v1_ledger_gets_tool_call_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "l.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, profile TEXT, session_id TEXT, task_id TEXT, tool TEXT NOT NULL, action TEXT NOT NULL, evidence TEXT NOT NULL, path TEXT, git_root TEXT, rel_path TEXT, cwd TEXT, path_guessed INTEGER NOT NULL DEFAULT 0, detail TEXT, source TEXT NOT NULL DEFAULT 'hook');"
                f"INSERT INTO events(ts, tool, action, evidence, path) VALUES ({time.time()}, 'write_file', 'write', 'documented', 'x');"
            )
            conn.commit()
            conn.close()
            l = ledger.Ledger(db)
            l.record(ledger.build_rows("write_file", {"path": "/abs/y"}, "{}", cwd=None, profile="p", session_id="s", task_id=None, tool_call_id="cid"))
            self.assertEqual(l.known_tool_call_ids(), {"cid"})
            self.assertEqual(len(l.query()), 2)


if __name__ == "__main__":
    unittest.main()

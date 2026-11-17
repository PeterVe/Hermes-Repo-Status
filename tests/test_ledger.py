"""Tests for the F2 ledger (extraction, storage, grouping) and its hook wiring."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gitprobe  # noqa: E402
import ledger  # noqa: E402
import render  # noqa: E402

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=GIT_ENV)


class ExtractTests(unittest.TestCase):
    def test_write_file(self):
        ev = ledger.extract_events("write_file", {"path": "a.txt", "content": "SECRET"}, json.dumps({"bytes_written": 6}))
        self.assertEqual(ev, [{"action": "write", "path": "a.txt", "evidence": "documented"}])

    def test_failed_write_is_ignored(self):
        self.assertEqual(ledger.extract_events("write_file", {"path": "a"}, json.dumps({"error": "denied"})), [])

    def test_patch_prefers_result_paths(self):
        res = json.dumps({"success": True, "files_modified": ["C:/r/a.py"], "files_created": ["C:/r/b.py"], "files_deleted": []})
        ev = ledger.extract_events("patch", {"path": "a.py"}, res)
        self.assertEqual([(e["action"], e["path"]) for e in ev], [("patch", "C:/r/a.py"), ("create", "C:/r/b.py")])

    def test_patch_v4a_from_args(self):
        patch = "*** Begin Patch\n*** Update File: src/x.py\n@@\n-a\n+b\n*** Add File: new.md\n+hi\n*** Delete File: old.txt\n*** End Patch"
        ev = ledger.extract_events("patch", {"mode": "patch", "patch": patch}, json.dumps({"success": True}))
        self.assertEqual([(e["action"], e["path"]) for e in ev], [("patch", "src/x.py"), ("create", "new.md"), ("delete", "old.txt")])

    def test_patch_no_change(self):
        self.assertEqual(ledger.extract_events("patch", {"path": "a"}, json.dumps({"success": True, "no_change": True})), [])

    def test_terminal_is_probable_and_redacted(self):
        ev = ledger.extract_events("terminal", {"command": "curl -H 'x' https://tok@h.io && export API_KEY=abc123", "workdir": "C:/w"}, "{}")
        self.assertEqual(ev[0]["evidence"], "probable")
        self.assertEqual(ev[0]["path"], "C:/w")
        self.assertNotIn("abc123", ev[0]["detail"])
        self.assertNotIn("tok@", ev[0]["detail"])

    def test_execute_code_first_line_only(self):
        ev = ledger.extract_events("execute_code", {"code": "import os\nopen('x','w').write('PASSWORD')"}, "{}")
        self.assertEqual(ev[0]["detail"], "import os")

    def test_mcp_write_tool(self):
        ev = ledger.extract_events("mcp__fs__write_file", {"path": "/tmp/x"}, "{}")
        self.assertEqual(ev[0]["path"], "/tmp/x")
        self.assertEqual(ledger.extract_events("mcp__fs__read_file", {"path": "/tmp/x"}, "{}"), [])

    def test_mapped_drive_and_unc_compare_equal(self):
        if os.name != "nt":
            self.skipTest("Windows drive mapping")
        saved = dict(ledger._drive_cache)
        try:
            ledger._drive_cache["Q:"] = "\\\\server\\share"
            self.assertTrue(ledger._inside("Q:\\proj\\a.py", "\\\\server\\share\\proj"))
            self.assertTrue(ledger._inside("\\\\server\\share\\proj\\a.py", "Q:\\proj"))
            self.assertFalse(ledger._inside("C:\\other\\a.py", "Q:\\proj"))
            self.assertTrue(ledger._inside("c:\\Temp\\x", "C:\\temp"))
        finally:
            ledger._drive_cache.clear()
            ledger._drive_cache.update(saved)

    def test_other_tools_ignored(self):
        self.assertEqual(ledger.extract_events("read_file", {"path": "a"}, "{}"), [])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.repo = self.base / "repo"
        git(self.base, "init", "-q", "-b", "main", str(self.repo))
        (self.repo / "keep.txt").write_text("k\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "init")
        self.other = self.base / "elsewhere"
        self.other.mkdir()
        ledger._root_cache.clear()
        self.db = ledger.Ledger(self.base / "shared" / "ledger.db")

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, tool, args, result="{}", cwd=None, session="s1", ts=None):
        return ledger.build_rows(tool, args, result, cwd=str(cwd or self.repo), profile="default", session_id=session, task_id="t", now=ts)

    def test_relative_path_resolved_and_root_found(self):
        (self.repo / "src").mkdir()
        r = self.rows("write_file", {"path": "src/a.py"})[0]
        self.assertEqual(Path(r["path"]), (self.repo / "src" / "a.py"))
        self.assertEqual(Path(r["git_root"]), self.repo)
        self.assertEqual(r["rel_path"], "src/a.py")
        self.assertEqual(r["path_guessed"], 1)

    def test_absolute_path_not_guessed(self):
        r = self.rows("write_file", {"path": str(self.other / "x.txt")})[0]
        self.assertEqual(r["path_guessed"], 0)
        self.assertIsNone(r["git_root"])

    def test_record_query_and_session_filter(self):
        self.db.record(self.rows("write_file", {"path": "a.txt"}, session="s1"))
        self.db.record(self.rows("write_file", {"path": "b.txt"}, session="s2"))
        self.assertEqual(len(self.db.query()), 2)
        self.assertEqual([Path(r["path"]).name for r in self.db.query(session_id="s1")], ["a.txt"])
        self.assertEqual(self.db.query(since=time.time() + 60), [])

    def test_query_missing_db(self):
        self.assertEqual(ledger.Ledger(self.base / "none.db").query(), [])

    def test_prune(self):
        db = ledger.Ledger(self.base / "p.db", retention_days=1)
        db.record(self.rows("write_file", {"path": "old.txt"}, ts=time.time() - 3 * 86400))
        db._last_prune = 0
        db.record(self.rows("write_file", {"path": "new.txt"}))
        self.assertEqual([Path(r["path"]).name for r in db.query()], ["new.txt"])

    def test_summary_groups_outside_cwd_and_observed(self):
        (self.repo / "dirty.txt").write_text("x\n", encoding="utf-8")
        self.db.record(self.rows("write_file", {"path": "dirty.txt"}))
        self.db.record(self.rows("patch", {"path": "keep.txt"}, json.dumps({"success": True})))
        self.db.record(self.rows("write_file", {"path": str(self.other / "loose.txt")}))
        self.db.record(self.rows("terminal", {"command": "npm install"}))
        events = self.db.query()

        def status(root):
            return gitprobe.probe(root, check_remote_enabled=False)

        data = ledger.summarize_events(events, cwd=str(self.repo), status_for_root=status)
        groups = data["groups"]
        self.assertEqual(len(groups), 2)
        self.assertTrue(groups[0]["outside_cwd"])  # outside first
        self.assertFalse(groups[0]["in_repo"])
        repo_group = groups[1]
        self.assertFalse(repo_group["outside_cwd"])
        files = {f["rel_path"]: f for f in repo_group["files"]}
        self.assertTrue(files["dirty.txt"]["still_uncommitted"])
        self.assertEqual(files["dirty.txt"]["evidence"], "documented+observed")
        self.assertFalse(files["keep.txt"]["still_uncommitted"])
        self.assertEqual(len(repo_group["commands"]), 1)
        text = render.render_touched(data, label_text="test")
        self.assertIn("outside cwd", text)
        self.assertIn("dirty.txt", text)
        self.assertIn("npm install", text)

    def test_render_empty(self):
        self.assertIn("no writes yet", render.render_touched({"groups": []}))


class HookWiringTests(unittest.TestCase):
    """Load the package like Hermes does and drive the registered hook."""

    def test_hook_records_into_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = importlib.util.spec_from_file_location("rs_pkg_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
            pkg = importlib.util.module_from_spec(spec)
            sys.modules["rs_pkg_test"] = pkg
            spec.loader.exec_module(pkg)
            pkg._ledger = ledger.Ledger(Path(tmp) / "l.db")
            pkg._session_cwd = lambda: Path(tmp)

            class Ctx:
                profile_name = "default"
                hooks, tools, cmds = {}, {}, {}

                def get_config(self, key, default=None):
                    return default

                def register_tool(self, **kw):
                    self.tools[kw["name"]] = kw

                def register_hook(self, name, fn):
                    self.hooks[name] = fn

                def register_command(self, name, handler, **kw):
                    self.cmds[name] = handler

                def register_cli_command(self, **kw):
                    pass

            ctx = Ctx()
            pkg.register(ctx)
            self.assertIn("hermes_touched_paths", ctx.tools)
            hook = ctx.hooks["post_tool_call"]
            hook(tool_name="write_file", args={"path": "x.txt", "content": "c"}, result='{"bytes_written": 1}', task_id="t", session_id="sess-1", duration_ms=3)
            hook(tool_name="read_file", args={"path": "y.txt"}, result="{}", task_id="t", session_id="sess-1")
            hook(tool_name="write_file", args=None, result=None)  # must not raise
            rows = pkg._ledger.query()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["session_id"], "sess-1")
            out = ctx.cmds["repo"]("touched", session_id="sess-1")
            self.assertIn("x.txt", out)
            tool_out = json.loads(ctx.tools["hermes_touched_paths"]["handler"]({}, session_id="sess-1"))
            self.assertEqual(tool_out["groups"][0]["file_count"], 1)
            sys.modules.pop("rs_pkg_test", None)


if __name__ == "__main__":
    unittest.main()

"""Export safety tests; every writable path belongs to a temporary fixture."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.source = self.base / "project"
        self.source.mkdir()
        (self.source / "hello.txt").write_text("hello\n", encoding="utf-8")
        (self.source / ".env.local").write_text("SECRET=value", encoding="utf-8")
        self.out = self.base / "exports"
        self.settings = {"export_dir": str(self.out), "hermes_home": str(self.home),
                         "ledger_path": str(self.home / "ledger.db")}
        self.project = {"id": "p1", "name": "demo"}

    def exporter(self):
        try:
            import export
        except ImportError:
            self.fail("export module is not implemented")
        return export

    def run_export(self, **kwargs):
        return self.exporter().start(self.settings, project=self.project,
                                     directories=[str(self.source)], background=False, **kwargs)

    def git(self, cwd, *args):
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(cwd), *args],
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
                                capture_output=True, check=True)
        return result.stdout.decode("utf-8", "replace").strip()

    def make_repo(self):
        self.server = self.base / "server.git"
        self.git(self.base, "init", "--bare", str(self.server))
        self.git(self.source, "init")
        self.git(self.source, "config", "user.name", "Export Test")
        self.git(self.source, "config", "user.email", "test@example.invalid")
        self.git(self.source, "add", "hello.txt")
        self.git(self.source, "commit", "-m", "initial")
        self.git(self.source, "remote", "add", "origin", str(self.server))
        self.git(self.source, "push", "origin", "HEAD")

    def test_dry_run_estimates_without_any_writes(self):
        result = self.run_export(dry_run=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["state"], "dry_run")
        self.assertGreaterEqual(result["estimated_bytes"], 6)
        self.assertFalse(self.out.exists())
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [])
        self.assertTrue(any(x["reason"] == "secret" for x in result["omitted"]))

    def test_real_server_archive_manifest_and_cleanup(self):
        self.make_repo()
        (self.source / "hello.txt").write_text("changed\n", encoding="utf-8")
        (self.source / "new.txt").write_text("untracked\n", encoding="utf-8")
        before = self.git(self.source, "status", "--porcelain=v1")
        progress = []
        result = self.run_export(progress=progress.append)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["state"], "completed")
        self.assertFalse(Path(result["work_dir"]).exists())
        self.assertEqual(before, self.git(self.source, "status", "--porcelain=v1"))
        self.assertTrue(progress)
        with zipfile.ZipFile(result["archive"]) as archive:
            self.assertIsNone(archive.testzip())
            names = archive.namelist()
            self.assertIn("git/project/server.bundle", names)
            self.assertIn("git/project/local.bundle", names)
            self.assertIn("folders/project/new.txt", names)
            self.assertNotIn("folders/project/.env.local", names)
            manifest = json.loads(archive.read("manifest.json"))
            self.assertNotEqual(manifest["hermes_version"], "unknown")
            self.assertEqual(set(names), {f["path"] for f in manifest["files"]} | {"manifest.json"})
            import hashlib
            for entry in manifest["files"]:
                data = archive.read(entry["path"])
                self.assertEqual(entry["size"], len(data))
                self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
            for name in ("project", "env", "sessions", "touched", "settings", "llm-calls", "scratch"):
                self.assertIn("hermes/" + name + ".json", names)
            self.assertIn("changed", archive.read("git/project/uncommitted.diff").decode())
        self.assertEqual(len(self.exporter().list_exports(self.settings, self.project)["exports"]), 1)


    def test_secret_history_omits_bundles_unless_explicitly_enabled(self):
        self.make_repo()
        (self.source / "auth.json").write_text('{"token":"private"}')
        self.git(self.source, "add", "auth.json")
        self.git(self.source, "commit", "-m", "secret")
        self.git(self.source, "push", "origin", "HEAD")
        self.git(self.source, "rm", "auth.json")
        self.git(self.source, "commit", "-m", "remove secret")
        result = self.run_export()
        self.assertTrue(result["ok"], result)
        with zipfile.ZipFile(result["archive"]) as archive:
            self.assertNotIn("git/project/local.bundle", archive.namelist())
            self.assertNotIn("git/project/server.bundle", archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertTrue(any("secret" in w for w in manifest["warnings"]))
        self.settings["export_include_secrets"] = True
        result = self.run_export()
        self.assertTrue(result["ok"], result)
        with zipfile.ZipFile(result["archive"]) as archive:
            self.assertIn("git/project/local.bundle", archive.namelist())
            self.assertIn("git/project/server.bundle", archive.namelist())
            self.assertIn("folders/project/.env.local", archive.namelist())

    def test_secret_matching_is_by_real_secret_file_names(self):
        secret = self.exporter()._secret
        for path in (".env", ".env.local", "config/.env.production", "certs/server.pem", "tls.key", "id_rsa",
                     "id_ed25519", "auth.json", "deploy/credentials.json", "secrets.yaml", ".git-credentials", "C:\\x\\.netrc"):
            self.assertTrue(secret(path), path)
        for path in ("crates/fleet/src/agent/credentials.rs", "signalhost/secretbox.go", ".github/workflows/secret-scan.yml",
                     "docker/show-admin-credentials.sh", "credentials/README.md", "secrets/notes.txt", ".env.example",
                     ".env.sample", "id_rsa.pub", "keyboard.rs", "src/monkey.py"):
            self.assertFalse(secret(path), path)

    def test_code_named_like_secrets_keeps_files_and_bundles(self):
        self.make_repo()
        code = self.source / "src" / "agent"
        code.mkdir(parents=True)
        (code / "credentials.rs").write_text("pub struct Credentials;\n")
        (code / "secretbox.go").write_text("package signalhost\n")
        (self.source / ".env.example").write_text("API_URL=\n")
        self.git(self.source, "add", "src", ".env.example")
        self.git(self.source, "commit", "-m", "code")
        self.git(self.source, "push", "origin", "HEAD")
        result = self.run_export()
        self.assertTrue(result["ok"], result)
        with zipfile.ZipFile(result["archive"]) as archive:
            names = archive.namelist()
            self.assertIn("folders/project/src/agent/credentials.rs", names)
            self.assertIn("folders/project/src/agent/secretbox.go", names)
            self.assertIn("folders/project/.env.example", names)
            self.assertNotIn("folders/project/.env.local", names)
            self.assertIn("git/project/local.bundle", names)
            self.assertIn("git/project/server.bundle", names)


    def test_replacement_refs_cannot_hide_secret_history(self):
        self.make_repo()
        secret = self.source / "auth.json"
        secret.write_text("FIXTURE_PRIVATE_TOKEN", encoding="utf-8")
        self.git(self.source, "add", "auth.json")
        self.git(self.source, "commit", "-m", "contains secret")
        original = self.git(self.source, "rev-parse", "HEAD")
        safe_tree = self.git(self.source, "rev-parse", "HEAD~1^{tree}")
        replacement = self.git(self.source, "commit-tree", safe_tree, "-m", "replacement safe tree")
        self.git(self.source, "replace", original, replacement)
        warnings = []
        allowed = self.exporter()._bundle_allowed(self.source, "local.bundle", self.settings, warnings)
        self.assertFalse(allowed)
        self.assertTrue(warnings)

    def test_tree_tag_secret_cannot_leak_through_bundles(self):
        self.make_repo()
        secret = self.source / "auth.json"
        secret.write_text("FIXTURE_PRIVATE_TOKEN", encoding="utf-8")
        self.git(self.source, "add", "auth.json")
        tree = self.git(self.source, "write-tree")
        self.git(self.source, "tag", "tree-backup", tree)
        self.git(self.source, "push", "origin", "refs/tags/tree-backup")
        self.git(self.source, "reset", "--mixed", "HEAD")
        secret.unlink()
        result = self.run_export()
        self.assertTrue(result["ok"], result)
        with zipfile.ZipFile(result["archive"]) as archive:
            self.assertNotIn("git/project/local.bundle", archive.namelist())
            self.assertNotIn("git/project/server.bundle", archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertTrue(any("bundle_omitted" in warning for warning in manifest["warnings"]))

    def test_insufficient_disk_space_refuses_before_writes(self):
        module = self.exporter()
        with patch.object(module.shutil, "disk_usage", return_value=type("Usage", (), {"free": 1})()):
            result = self.run_export()
        self.assertFalse(result["ok"], result)
        self.assertIn("free=1", result["error"])
        self.assertIn("required=", result["error"])
        self.assertFalse(self.out.exists())


    def test_registry_and_metadata_are_read_only_and_session_scoped(self):
        projects = self.home / "projects.db"
        with closing(sqlite3.connect(projects)) as db, db:
            db.executescript("CREATE TABLE projects(id TEXT, name TEXT, primary_path TEXT);"
                             "CREATE TABLE project_folders(project_id TEXT, path TEXT);")
            db.execute("INSERT INTO projects VALUES(?,?,?)", ("p1", "registered", str(self.source)))
            db.execute("INSERT INTO project_folders VALUES(?,?)", ("p1", str(self.source)))
        with closing(sqlite3.connect(self.home / "state.db")) as db, db:
            db.executescript("CREATE TABLE sessions(id TEXT, title TEXT, cwd TEXT, source TEXT, started_at REAL, system_prompt TEXT);")
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)", ("s1", "Session", str(self.source), "cli", 1, "PRIVATE TRANSCRIPT"))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)", ("other", "Other", str(self.base), "cli", 1, "PRIVATE TRANSCRIPT"))
        import ledger, llm_log
        with closing(sqlite3.connect(self.home / "ledger.db")) as db, db:
            db.executescript(ledger._SCHEMA)
            db.execute("INSERT INTO events(ts,session_id,tool,action,evidence,path,cwd,category,detail) VALUES(?,?,?,?,?,?,?,?,?)",
                       (1, "s1", "write_file", "write", "test", str(self.source / "hello.txt"), str(self.source), "config", "PRIVATE TRANSCRIPT"))
        with closing(sqlite3.connect(self.home / "llm.db")) as db, db:
            db.executescript(llm_log._SCHEMA)
            db.execute("INSERT INTO calls(ts,kind,status,session_id,provider,model,prompt_tokens,context_length,error_message) VALUES(?,?,?,?,?,?,?,?,?)",
                       (1, "turn", "ok", "s1", "provider", "model", 12, 1000, "PRIVATE TRANSCRIPT"))
            db.execute("INSERT INTO content(call_id,ts,request_json,response_json) VALUES(?,?,?,?)", (1, 1, "PRIVATE TRANSCRIPT", "PRIVATE TRANSCRIPT"))
        before = {p.name: p.read_bytes() for p in self.home.glob("*.db")}
        result = self.exporter().start(self.settings, cwd=str(self.source), background=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["project"]["name"], "registered")
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.home.glob("*.db")})
        with zipfile.ZipFile(result["archive"]) as archive:
            sessions = json.loads(archive.read("hermes/sessions.json"))
            self.assertEqual([s["id"] for s in sessions], ["s1"])
            self.assertEqual(sessions[0]["context"]["max"], 1000)
            self.assertEqual(len(json.loads(archive.read("hermes/touched.json"))), 1)
            self.assertEqual(len(json.loads(archive.read("hermes/settings.json"))), 1)
            self.assertEqual(len(json.loads(archive.read("hermes/llm-calls.json"))), 1)
            self.assertNotIn(b"PRIVATE TRANSCRIPT", b"".join(archive.read(n) for n in archive.namelist()))


    def test_scratch_listing_opt_in_and_total_cap(self):
        task = self.home / "cache" / "scratch" / "task"
        task.mkdir(parents=True)
        (task / "work.txt").write_text("scratch")
        (task / ".env").write_text("never")
        (self.home / "disk.json").write_text(json.dumps({"scratch": [{"name": "task", "path": str(task), "size": 12,
                                                                    "project_cwd": str(self.source)}]}))
        result = self.run_export()
        self.assertTrue(result["ok"], result)
        with zipfile.ZipFile(result["archive"]) as archive:
            scratch = json.loads(archive.read("hermes/scratch.json"))
            self.assertEqual(scratch[0]["name"], "task")
            self.assertFalse(scratch[0]["included"])
            self.assertNotIn("hermes/work/task/work.txt", archive.namelist())
        self.settings.update(export_include_scratch=True, export_scratch_cap_mb=1)
        result = self.run_export()
        self.assertTrue(result["ok"], result)
        with zipfile.ZipFile(result["archive"]) as archive:
            self.assertIn("hermes/work/task/work.txt", archive.namelist())
            self.assertNotIn("hermes/work/task/.env", archive.namelist())
        self.settings["export_scratch_cap_mb"] = 0
        result = self.run_export()
        with zipfile.ZipFile(result["archive"]) as archive:
            self.assertNotIn("hermes/work/task/work.txt", archive.namelist())
            self.assertTrue(any(o["reason"] == "scratch_cap" for o in json.loads(archive.read("manifest.json"))["omitted"]))


    def test_cross_process_lock_blocks_a_second_real_export(self):
        self.out.mkdir()
        module = self.exporter()
        handle = module._acquire_lock(self.out)
        self.addCleanup(handle.close)
        code = ("import sys,json;sys.path.insert(0,sys.argv[1]);import export;"
                "print(json.dumps(export.start(json.loads(sys.argv[2]),project={'name':'demo'},"
                "directories=[sys.argv[3]],background=False)))")
        proc = subprocess.run([sys.executable, "-c", code, str(ROOT), json.dumps(self.settings), str(self.source)],
                              capture_output=True, text=True, timeout=45)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertFalse(result["ok"], result)
        self.assertIn("export_busy", result["error"])
        self.assertFalse((self.out / "_work").exists())
        result = self.run_export(dry_run=True)
        self.assertTrue(result["ok"], result)


    def test_posix_symlink_mode_is_rejected_without_windows_attributes(self):
        module = self.exporter()
        fake = os.stat_result((module.stat.S_IFLNK | 0o777, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        with patch.object(Path, "lstat", return_value=fake), patch.object(module.disk_usage, "_is_link", return_value=False):
            self.assertTrue(module._linked(self.source))


    def test_retention_keeps_only_own_project_archives_and_zero_keeps_all(self):
        first = self.run_export()
        second = self.run_export()
        self.assertTrue(first["ok"] and second["ok"])
        self.assertTrue(Path(first["archive"]).exists())
        foreign = self.out / "demo_2000-01-01_0000_PC.zip"
        foreign.write_text("not our archive")
        self.settings["export_keep_last"] = 1
        third = self.run_export()
        self.assertTrue(third["ok"], third)
        self.assertEqual(len(self.exporter().list_exports(self.settings, self.project)["exports"]), 1)
        self.assertTrue(Path(third["archive"]).exists())
        self.assertFalse(Path(first["archive"]).exists())
        self.assertFalse(Path(second["archive"]).exists())
        self.assertTrue(foreign.exists())


    def test_prs_payload_contains_per_branch_requests(self):
        self.make_repo()
        branches = {"ok": True, "branches": [{"name": "main", "prs": [{"number": 7, "title": "Fix", "url": "https://example.invalid/pr/7"}]}],
                    "prs": {"status": "ok", "count": 1}}
        with patch.object(self.exporter().gitprobe, "remote_branches", return_value=branches):
            result = self.run_export()
        self.assertTrue(result["ok"], result)
        with zipfile.ZipFile(result["archive"]) as archive:
            prs = json.loads(archive.read("git/project/prs.json"))
            self.assertIsInstance(prs, list)
            self.assertEqual(prs[0]["number"], 7)
            self.assertEqual(prs[0]["branch"], "main")


    def test_cleanup_refuses_unmarked_outside_and_linked_work(self):
        module = self.exporter()
        work = self.out / "_work" / "job"
        work.mkdir(parents=True)
        with self.assertRaises(ValueError):
            module._safe_remove_work(work, self.out)
        marker = {"exporter": "repo-status", "token": "test"}
        outside = self.base / "outside"
        outside.mkdir()
        (outside / ".repo-status-export").write_text(json.dumps(marker))
        with self.assertRaises(ValueError):
            module._safe_remove_work(outside, self.out)
        (work / ".repo-status-export").write_text(json.dumps(marker))
        with patch.object(module, "_linked", return_value=True):
            with self.assertRaises(ValueError):
                module._safe_remove_work(work, self.out)
        self.assertTrue(work.exists() and outside.exists())

    def test_verification_failure_preserves_marked_work_and_part(self):
        module = self.exporter()
        with patch.object(module, "_verify_archive", side_effect=ValueError("archive_sha256")):
            result = self.run_export()
        self.assertFalse(result["ok"], result)
        work = Path(result["work_dir"])
        self.assertTrue((work / ".repo-status-export").is_file())
        self.assertFalse(list(self.out.glob("*.zip")))
        self.assertEqual(len(list(self.out.glob("*.zip.part"))), 1)

    def test_unavailable_server_warns_but_keeps_local_bundle(self):
        self.make_repo()
        self.git(self.source, "remote", "set-url", "origin", str(self.base / "missing.git"))
        result = self.run_export()
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("server_unavailable" in warning for warning in result["warnings"]))
        with zipfile.ZipFile(result["archive"]) as archive:
            self.assertIn("git/project/local.bundle", archive.namelist())
            self.assertNotIn("git/project/server.bundle", archive.namelist())

    def test_standalone_and_package_load_share_status(self):
        module = self.exporter()
        spec = importlib.util.spec_from_file_location("another_package_export", ROOT / "export.py")
        other = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(other)
        result = self.run_export(dry_run=True)
        self.assertEqual(other.status(), result)
        with module._shared.lock:
            module._shared.job["running"] = True
        try:
            result = other.start(self.settings, directories=[str(self.source)], background=False)
            self.assertFalse(result["ok"])
            self.assertIn("export_busy", result["error"])
        finally:
            with module._shared.lock:
                module._shared.job["running"] = False

    def test_archive_verifier_rejects_modified_digest(self):
        result = self.run_export()
        with zipfile.ZipFile(result["archive"]) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        manifest["files"][0]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            self.exporter()._verify_archive(result["archive"], manifest, [])

    def test_non_git_excludes_build_folders_and_unsafe_names(self):
        (self.source / "node_modules").mkdir()
        (self.source / "node_modules" / "ignored").write_text("ignore")
        (self.source / "nested").mkdir()
        (self.source / "nested" / "service.key").write_text("ignore")
        self.project["name"] = "../CON:/evil"
        result = self.run_export()
        self.assertTrue(result["ok"], result)
        self.assertEqual(Path(result["archive"]).parent, self.out)
        with zipfile.ZipFile(result["archive"]) as archive:
            self.assertFalse(any("node_modules" in n or "service.key" in n or ".." in Path(n).parts for n in archive.namelist()))


    def test_public_resolve_and_invalid_inputs_return_dicts_without_writes(self):
        module = self.exporter()
        resolved = module.resolve_project(self.settings, project=self.project, directories=[str(self.source)])
        self.assertTrue(resolved["ok"], resolved)
        self.assertEqual(resolved["directories"], [str(self.source)])
        for kwargs in ({"project": []}, {"directories": "bad"}, {"cwd": "relative"}, {"dry_run": "false"}):
            with self.subTest(kwargs=kwargs):
                result = module.start(self.settings, background=False, **kwargs)
                self.assertFalse(result["ok"], result)
                self.assertFalse(result["running"])
        self.assertFalse(self.out.exists())


    def test_wal_reads_create_no_source_sidecars_even_in_dry_run(self):
        dbpath = self.home / "state.db"
        with closing(sqlite3.connect(dbpath)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE sessions(id TEXT, title TEXT, cwd TEXT)")
            db.execute("INSERT INTO sessions VALUES(?,?,?)", ("s1", "wal", str(self.source)))
        before = {p.name: p.read_bytes() for p in self.home.iterdir()}
        result = self.run_export(dry_run=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.home.iterdir()})
        result = self.run_export()
        self.assertTrue(result["ok"], result)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.home.iterdir()})
        with zipfile.ZipFile(result["archive"]) as archive:
            self.assertEqual(json.loads(archive.read("hermes/sessions.json"))[0]["id"], "s1")


    def test_unborn_repo_exports_staged_patch_against_empty_tree(self):
        self.git(self.source, "init")
        self.git(self.source, "add", "hello.txt")
        result = self.run_export()
        self.assertTrue(result["ok"], result)
        with zipfile.ZipFile(result["archive"]) as archive:
            diff = archive.read("git/project/uncommitted.diff").decode()
            self.assertIn("+hello", diff)
            self.assertNotIn(".env.local", diff)

    def test_active_wal_metadata_is_read_from_private_copy(self):
        dbpath = self.home / "state.db"
        with closing(sqlite3.connect(dbpath)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE sessions(id TEXT, title TEXT, cwd TEXT)")
            db.commit()
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("INSERT INTO sessions VALUES(?,?,?)", ("live", "latest", str(self.source)))
            db.commit()
            import hashlib
            snapshot = lambda: {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.home.iterdir()}
            before = snapshot()
            dry = self.run_export(dry_run=True)
            self.assertTrue(dry["ok"], dry)
            self.assertTrue(any("ignores_wal" in warning for warning in dry["warnings"]))
            self.assertEqual(before, snapshot())
            result = self.run_export()
            self.assertTrue(result["ok"], result)
            self.assertEqual(before, snapshot())
            with zipfile.ZipFile(result["archive"]) as archive:
                self.assertEqual(json.loads(archive.read("hermes/sessions.json"))[0]["id"], "live")


    def test_probe_git_disables_helpers_and_uses_network_environment(self):
        module = self.exporter()
        completed = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        with patch.object(module.subprocess, "run", return_value=completed) as run, patch.object(module.gitprobe, "_base_env", wraps=module.gitprobe._base_env) as env:
            module.gitprobe.run_git(self.source, "diff", "--numstat")
        env.assert_called_once_with(network=True)
        command = run.call_args.args[0]
        for flag in ("--no-optional-locks", "--no-replace-objects", "--no-ext-diff", "--no-textconv"):
            self.assertIn(flag, command)
        self.assertEqual(run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")


    def test_completion_callback_cannot_start_an_overlapping_job(self):
        nested = []
        def progress(value):
            if value.get("state") == "completed":
                nested.append(self.run_export(dry_run=True))
        result = self.run_export(progress=progress)
        self.assertEqual(result["state"], "completed")
        self.assertFalse(nested[0]["ok"])
        self.assertIn("export_busy", nested[0]["error"])


    def test_disk_estimate_includes_private_database_snapshots(self):
        with closing(sqlite3.connect(self.home / "state.db")) as db, db:
            db.execute("CREATE TABLE padding(value BLOB)")
            db.execute("INSERT INTO padding VALUES(zeroblob(16384))")
        with patch.object(self.exporter().shutil, "disk_usage", return_value=type("Usage", (), {"free": 1024 ** 3 + 1000})()):
            result = self.run_export()
        self.assertFalse(result["ok"], result)
        self.assertIn("insufficient_space", result["error"])
        self.assertFalse(self.out.exists())


    def test_wal_only_project_registry_controls_archive_name(self):
        with closing(sqlite3.connect(self.home / "projects.db")) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("CREATE TABLE projects(id TEXT, name TEXT, primary_path TEXT);"
                             "CREATE TABLE project_folders(project_id TEXT, path TEXT);")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("INSERT INTO projects VALUES(?,?,?)", ("p1", "registered", str(self.source)))
            db.execute("INSERT INTO project_folders VALUES(?,?)", ("p1", str(self.source)))
            db.commit()
            result = self.exporter().start(self.settings, cwd=str(self.source), background=False)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["project"]["name"], "registered")
            self.assertTrue(Path(result["archive"]).name.startswith("registered_"), result)


    def test_wal_registry_folder_expansion_rechecks_disk_before_copy(self):
        extra = self.base / "extra"
        extra.mkdir()
        (extra / "large.bin").write_bytes(b"x" * (2 * 1024 ** 2))
        with closing(sqlite3.connect(self.home / "projects.db")) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("CREATE TABLE projects(id TEXT, name TEXT, primary_path TEXT);"
                             "CREATE TABLE project_folders(project_id TEXT, path TEXT);")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("INSERT INTO projects VALUES(?,?,?)", ("p1", "registered", str(self.source)))
            db.executemany("INSERT INTO project_folders VALUES(?,?)", [("p1", str(self.source)), ("p1", str(extra))])
            db.commit()
            with patch.object(self.exporter().shutil, "disk_usage", return_value=type("Usage", (), {"free": 1024 ** 3 + 1024 ** 2})()):
                result = self.exporter().start(self.settings, cwd=str(self.source), background=False)
            self.assertFalse(result["ok"], result)
            self.assertIn("insufficient_space", result["error"])
            self.assertTrue((Path(result["work_dir"]) / ".repo-status-export").exists())


if __name__ == "__main__":
    unittest.main()

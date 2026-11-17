"""Tests for gitprobe/render against real temporary git repositories.

Run: <hermes venv python> -m unittest discover -s tests -v   (from the plugin root)
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gitprobe  # noqa: E402
import render  # noqa: E402

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=GIT_ENV)


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class ParserTests(unittest.TestCase):
    def test_redact_https_token(self):
        self.assertEqual(gitprobe.redact_url("https://ghp_abc@github.com/o/r.git"), "https://***@github.com/o/r.git")
        self.assertEqual(gitprobe.redact_url("https://user:pw@gitlab.com/o/r"), "https://***@gitlab.com/o/r")
        self.assertEqual(gitprobe.redact_url("https://github.com/o/r.git"), "https://github.com/o/r.git")
        self.assertEqual(gitprobe.redact_url("ssh://git@host/o/r.git"), "ssh://git@host/o/r.git")
        self.assertEqual(gitprobe.redact_url("git@github.com:o/r.git"), "git@github.com:o/r.git")

    def test_redact_text(self):
        self.assertNotIn("secret", gitprobe.redact_text("fatal: unable to access 'https://secret@github.com/x'"))

    def test_classify(self):
        cases = {
            "https://github.com/o/r.git": ("github", "o/r", "https://github.com/o/r"),
            "git@github.com:o/r.git": ("github", "o/r", "https://github.com/o/r"),
            "ssh://git@ssh.github.com:443/o/r.git": ("github", "o/r", "https://github.com/o/r"),
            "https://gitlab.com/g/sub/r.git": ("gitlab", "g/sub/r", "https://gitlab.com/g/sub/r"),
            "https://gitlab.firma.sk/g/r": ("gitlab", "g/r", "https://gitlab.firma.sk/g/r"),
            "https://codeberg.org/o/r": ("forgejo", "o/r", "https://codeberg.org/o/r"),
            "https://git.example.org/o/r": ("other", "o/r", "https://git.example.org/o/r"),
            r"C:\repos\bare.git": ("local", None, None),
            "/srv/git/r.git": ("local", None, None),
        }
        for url, (kind, repo, web) in cases.items():
            with self.subTest(url=url):
                d = gitprobe.describe_remote("origin", url)
                self.assertEqual(d["kind"], kind)
                if repo:
                    self.assertEqual(d["repo"], repo)
                self.assertEqual(d["web_url"], web)

    def test_host_kinds_override(self):
        d = gitprobe.describe_remote("o", "https://git.firma.sk/a/b", {"git.firma.sk": "forgejo"})
        self.assertEqual(d["kind"], "forgejo")

    def test_parse_status_v2(self):
        raw = "\0".join(
            [
                "# branch.oid abc123",
                "# branch.head feature/x",
                "# branch.upstream origin/feature/x",
                "# branch.ab +2 -1",
                "1 M. N... 100644 100644 100644 aaa bbb staged.txt",
                "1 .M N... 100644 100644 100644 aaa bbb work tree file.txt",
                "2 R. N... 100644 100644 100644 aaa bbb R100 new.txt",
                "old.txt",
                "u UU N... 100644 100644 100644 100644 a b c conflict.txt",
                "? nový súbor.txt",
                "! ignored.log",
                "",
            ]
        )
        p = gitprobe.parse_status_v2(raw)
        self.assertEqual(p["branch"]["head"], "feature/x")
        self.assertEqual((p["branch"]["ahead"], p["branch"]["behind"]), (2, 1))
        c = p["changes"]
        self.assertEqual(c["total"], 5)
        self.assertEqual((c["staged"], c["unstaged"], c["untracked"], c["conflicted"]), (2, 1, 1, 1))
        paths = [f["path"] for f in c["files"]]
        self.assertIn("work tree file.txt", paths)
        self.assertIn("nový súbor.txt", paths)
        self.assertNotIn("ignored.log", paths)
        renamed = next(f for f in c["files"] if f["path"] == "new.txt")
        self.assertEqual(renamed["orig_path"], "old.txt")

    def test_parse_status_work_prefixes(self):
        raw = "\0".join(["? .hermes/a.md", "? .hermesx/b.md", "? src/c.py", "1 .M N... 100644 100644 100644 a b .hermes/t.md", ""])
        p = gitprobe.parse_status_v2(raw, work_prefixes=(".hermes",))
        self.assertEqual(p["changes"]["total"], 2)  # .hermesx/ is not .hermes/
        self.assertEqual(p["work_files"]["total"], 2)

    def test_parse_status_cap(self):
        raw = "\0".join(f"? f{i}" for i in range(10)) + "\0"
        c = gitprobe.parse_status_v2(raw, file_cap=3)["changes"]
        self.assertEqual((c["total"], len(c["files"]), c["truncated"]), (10, 3, 7))

    def test_parse_numstat(self):
        raw = "3\t1\ta.txt\0-\t-\timg.png\0" + "0\t0\t\0old.txt\0new.txt\0"
        d = gitprobe.parse_numstat_z(raw)
        self.assertEqual((d["count"], d["added"], d["removed"]), (3, 3, 1))
        self.assertTrue(d["files"][1]["binary"])
        self.assertEqual(d["files"][2]["path"], "new.txt")
        self.assertEqual(d["files"][2]["orig_path"], "old.txt")


class RepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.server = self.base / "server.git"
        self.repo = self.base / "work"
        git(self.base, "init", "-q", "--bare", "-b", "main", str(self.server))
        git(self.base, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "config", "user.name", "Lokálny Autor")
        git(self.repo, "config", "user.email", "local@example.invalid")
        write(self.repo / "a.txt", "one\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "remote", "add", "origin", str(self.server))
        git(self.repo, "push", "-q", "-u", "origin", "main")
        gitprobe.clear_remote_cache()

    def tearDown(self):
        self.tmp.cleanup()

    def probe(self, **kw):
        kw.setdefault("remote_cache_seconds", 0)
        return gitprobe.probe(self.repo, **kw)

    def test_not_a_repo(self):
        plain = self.base / "plain"
        plain.mkdir()
        p = gitprobe.probe(plain)
        self.assertFalse(p["ok"])
        self.assertEqual(p["error"], "not_a_repo")

    def test_missing_path(self):
        p = gitprobe.probe(self.base / "nope")
        self.assertFalse(p["ok"])
        self.assertEqual(p["error"], "git_error")

    def test_clean_and_up_to_date(self):
        p = self.probe()
        self.assertTrue(p["ok"], p)
        self.assertEqual(Path(p["repo"]["root"]).resolve(), self.repo.resolve())
        self.assertEqual(p["branch"]["head"], "main")
        self.assertEqual(p["branch"]["upstream"], "origin/main")
        self.assertEqual(p["remotes"][0]["kind"], "local")
        self.assertEqual(p["identity"]["name"]["value"], "Lokálny Autor")
        self.assertEqual(p["identity"]["name"]["scope"], "local")
        self.assertEqual(p["last_commit"]["subject"], "init")
        self.assertEqual(p["remote_check"]["status"], "up_to_date")
        self.assertTrue(p["summary"]["current"])
        self.assertEqual(p["summary"]["tone"], "good")

    def test_probe_from_subfolder_and_file(self):
        write(self.repo / "sub" / "b.txt", "x\n")
        for target in (self.repo / "sub", self.repo / "sub" / "b.txt"):
            p = gitprobe.probe(target, check_remote_enabled=False)
            self.assertTrue(p["ok"])
            self.assertEqual(Path(p["repo"]["root"]).resolve(), self.repo.resolve())

    def test_uncommitted_files_and_diffstat(self):
        write(self.repo / "a.txt", "one\ntwo\nthree\n")
        write(self.repo / "nový.txt", "hello\n")
        write(self.repo / "staged.txt", "s\n")
        git(self.repo, "add", "staged.txt")
        p = self.probe()
        c = p["changes"]
        self.assertEqual(c["total"], 3)
        self.assertEqual((c["staged"], c["unstaged"], c["untracked"]), (1, 1, 1))
        self.assertIn("nový.txt", [f["path"] for f in c["files"]])
        stat = {f["path"]: f for f in p["diffstat"]["files"]}
        self.assertEqual((stat["a.txt"]["added"], stat["a.txt"]["removed"]), (2, 0))
        self.assertIn("staged.txt", stat)
        self.assertNotIn("nový.txt", stat)  # untracked files are not in a diff
        self.assertFalse(p["summary"]["current"])
        self.assertIn("uncommitted", p["summary"]["reasons"])

    def test_local_ahead(self):
        write(self.repo / "a.txt", "changed\n")
        git(self.repo, "commit", "-q", "-am", "local only")
        p = self.probe()
        self.assertEqual(p["remote_check"]["status"], "local_ahead")
        self.assertEqual(p["remote_check"]["ahead"], 1)
        self.assertIn("unpushed", p["summary"]["reasons"])

    def _push_from_other_clone(self):
        other = self.base / "other"
        git(self.base, "clone", "-q", str(self.server), str(other))
        write(other / "c.txt", "from elsewhere\n")
        git(other, "add", "c.txt")
        git(other, "commit", "-q", "-m", "remote change")
        git(other, "push", "-q")

    def test_remote_newer_without_fetch(self):
        self._push_from_other_clone()
        p = self.probe()
        self.assertEqual(p["remote_check"]["status"], "remote_newer")
        self.assertIsNone(p["remote_check"]["behind"])
        self.assertIn("server_newer", p["summary"]["reasons"])
        self.assertEqual(p["summary"]["tone"], "bad")

    def test_behind_after_fetch(self):
        self._push_from_other_clone()
        git(self.repo, "fetch", "-q")
        p = self.probe()
        self.assertEqual(p["remote_check"]["status"], "behind")
        self.assertEqual(p["remote_check"]["behind"], 1)
        self.assertIsNotNone(p["fetch"]["last_fetch"])

    def test_diverged(self):
        self._push_from_other_clone()
        git(self.repo, "fetch", "-q")
        write(self.repo / "a.txt", "mine\n")
        git(self.repo, "commit", "-q", "-am", "mine")
        p = self.probe()
        self.assertEqual(p["remote_check"]["status"], "diverged")
        self.assertEqual((p["remote_check"]["ahead"], p["remote_check"]["behind"]), (1, 1))

    def test_not_on_remote_and_no_upstream(self):
        git(self.repo, "switch", "-q", "-c", "feature")
        self.assertEqual(self.probe()["remote_check"]["status"], "no_upstream")
        git(self.repo, "push", "-q", "-u", "origin", "feature")
        git(self.server, "branch", "-D", "feature")
        self.assertEqual(self.probe()["remote_check"]["status"], "not_on_remote")

    def test_unreachable_remote_is_error_not_exception(self):
        git(self.repo, "remote", "set-url", "origin", str(self.base / "missing.git"))
        p = self.probe()
        self.assertTrue(p["ok"])
        self.assertEqual(p["remote_check"]["status"], "error")
        self.assertIn("remote_unreachable", p["summary"]["reasons"])

    def test_remote_cache(self):
        p1 = gitprobe.probe(self.repo, remote_cache_seconds=300)
        p2 = gitprobe.probe(self.repo, remote_cache_seconds=300)
        p3 = gitprobe.probe(self.repo, remote_cache_seconds=300, fresh=True)
        self.assertFalse(p1["remote_check"]["cached"])
        self.assertTrue(p2["remote_check"]["cached"])
        self.assertFalse(p3["remote_check"]["cached"])

    def test_skipped_remote(self):
        p = self.probe(check_remote_enabled=False)
        self.assertEqual(p["remote_check"]["status"], "skipped")

    def test_probe_remote_only(self):
        self.assertEqual(gitprobe.probe_remote(self.repo, remote_cache_seconds=0)["status"], "up_to_date")
        plain = self.base / "plain2"
        plain.mkdir()
        self.assertEqual(gitprobe.probe_remote(plain)["status"], "not_a_repo")

    def test_worktree(self):
        wt = self.base / "wt"
        git(self.repo, "worktree", "add", "-q", "-b", "wt-branch", str(wt))
        p = gitprobe.probe(wt, check_remote_enabled=False)
        self.assertTrue(p["repo"]["is_worktree"])
        self.assertEqual(len(p["repo"]["worktrees"]), 2)
        self.assertEqual(p["branch"]["head"], "wt-branch")

    def test_unborn_repo(self):
        fresh = self.base / "fresh"
        git(self.base, "init", "-q", "-b", "main", str(fresh))
        write(fresh / "x.txt", "x\n")
        p = gitprobe.probe(fresh)
        self.assertTrue(p["ok"], p)
        self.assertIsNone(p["last_commit"])
        self.assertEqual(p["remote_check"]["status"], "no_remote")
        self.assertEqual(p["changes"]["untracked"], 1)

    def test_probe_does_not_modify_repo(self):
        write(self.repo / "a.txt", "dirty\n")
        before = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain=v2", "--branch"], capture_output=True).stdout
        idx = (self.repo / ".git" / "index").stat().st_mtime_ns
        self.probe()
        after = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain=v2", "--branch"], capture_output=True).stdout
        self.assertEqual(before, after)
        self.assertEqual(idx, (self.repo / ".git" / "index").stat().st_mtime_ns)
        self.assertFalse((self.repo / ".git" / "FETCH_HEAD").exists())

    def test_full_diff_excludes_secrets(self):
        write(self.repo / ".env", "TOKEN=1\n")
        write(self.repo / "a.txt", "visible change\n")
        git(self.repo, "add", ".env")
        d = gitprobe.full_diff(self.repo)
        self.assertTrue(d["ok"])
        self.assertIn("visible change", d["diff"])
        self.assertNotIn("TOKEN", d["diff"])

    def test_hermes_work_files_reported_apart(self):
        write(self.repo / ".hermes" / "plans" / "plan.md", "plan\n")
        write(self.repo / ".hermes" / "sketch.txt", "s\n")
        p = self.probe()
        self.assertEqual(p["changes"]["total"], 0)  # no source changes
        self.assertEqual(p["work_files"]["total"], 2)
        self.assertEqual(p["work_files"]["dirs"], {".hermes/": 2})
        self.assertTrue(p["summary"]["current"])  # work material does not break "all committed"
        self.assertIn("Hermes work files", render.render(p))
        write(self.repo / "a.txt", "real change\n")
        p = self.probe()
        self.assertEqual((p["changes"]["total"], p["work_files"]["total"]), (1, 2))
        self.assertIn("uncommitted", p["summary"]["reasons"])
        # switched off: everything counts as a change again
        p = self.probe(work_dirs=())
        self.assertEqual((p["changes"]["total"], p["work_files"]["total"]), (3, 0))

    def test_tracked_hermes_file_excluded_from_diffstat(self):
        write(self.repo / ".hermes" / "notes.md", "v1\n")
        git(self.repo, "add", ".hermes/notes.md")
        git(self.repo, "commit", "-q", "-m", "notes")
        git(self.repo, "push", "-q")
        write(self.repo / ".hermes" / "notes.md", "v2\nmore\n")
        p = self.probe()
        self.assertEqual(p["diffstat"]["count"], 0)
        self.assertEqual(p["work_files"]["total"], 1)

    def test_render(self):
        write(self.repo / "a.txt", "x\ny\n")
        text = render.render(self.probe())
        self.assertIn("Local repository", text)
        self.assertIn("Git user", text)
        self.assertIn("`main`", text)
        self.assertIn("a.txt", text)
        self.assertIn("uncommitted changes", text)
        self.assertIn("not inside a git repository", render.render(gitprobe.probe(self.base)))


class BranchesTests(unittest.TestCase):
    """Server branches against a local bare "server" (no network, no gh: PRs unsupported)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.server = self.base / "server.git"
        self.repo = self.base / "work"
        self.other = self.base / "other"
        git(self.base, "init", "-q", "--bare", "-b", "main", str(self.server))
        git(self.base, "init", "-q", "-b", "main", str(self.repo))
        write(self.repo / "a.txt", "one\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "remote", "add", "origin", str(self.server))
        git(self.repo, "push", "-q", "-u", "origin", "main")
        # a colleague pushes a branch we never fetched
        git(self.base, "clone", "-q", str(self.server), str(self.other))
        git(self.other, "checkout", "-q", "-b", "colleague/feature")
        write(self.other / "b.txt", "two\n")
        git(self.other, "add", "b.txt")
        git(self.other, "commit", "-q", "-m", "feature")
        git(self.other, "push", "-q", "origin", "colleague/feature")
        # our topic branch is one commit ahead of the server
        git(self.repo, "checkout", "-q", "-b", "topic")
        git(self.repo, "push", "-q", "-u", "origin", "topic")
        write(self.repo / "c.txt", "three\n")
        git(self.repo, "add", "c.txt")
        git(self.repo, "commit", "-q", "-m", "local work")
        git(self.repo, "branch", "only-here")
        gitprobe.clear_remote_cache()

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_ls_remote(self):
        raw = "ref: refs/heads/main\tHEAD\nabc\tHEAD\nabc\trefs/heads/main\ndef\trefs/heads/x/y\n"
        heads, default = gitprobe.parse_ls_remote(raw)
        self.assertEqual(default, "main")
        self.assertEqual(heads, {"main": "abc", "x/y": "def"})

    def test_branches(self):
        r = gitprobe.remote_branches(self.repo, cache_seconds=0)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["default"], "main")
        by = {b["name"]: b for b in r["branches"]}
        self.assertEqual(set(by), {"main", "topic", "colleague/feature"})
        self.assertEqual(r["branches"][0]["name"], "topic")  # current first
        self.assertTrue(by["topic"]["current"])
        self.assertEqual((by["topic"]["sync"], by["topic"]["ahead"], by["topic"]["behind"]), ("ahead", 1, 0))
        self.assertTrue(by["main"]["default"])
        self.assertEqual(by["main"]["sync"], "same")
        self.assertEqual(by["colleague/feature"]["sync"], "server_only")
        self.assertIsNone(by["colleague/feature"]["commit"])  # never fetched: no local commit
        self.assertEqual(by["main"]["commit"]["subject"], "init")
        self.assertEqual(r["local_only"], ["only-here"])
        self.assertEqual(r["prs"]["status"], "unsupported")
        text = render.render_branches(r)
        self.assertIn("colleague/feature", text)
        self.assertIn("only-here", text)

    def test_no_remote(self):
        git(self.repo, "remote", "remove", "origin")
        r = gitprobe.remote_branches(self.repo, cache_seconds=0)
        self.assertEqual(r["status"], "no_remote")

    def test_cache_and_unreachable(self):
        first = gitprobe.remote_branches(self.repo)
        self.assertFalse(first["cached"])
        self.assertTrue(gitprobe.remote_branches(self.repo)["cached"])
        git(self.repo, "remote", "set-url", "origin", str(self.base / "gone.git"))
        r = gitprobe.remote_branches(self.repo, fresh=True)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["branches"], [])


if __name__ == "__main__":
    unittest.main()

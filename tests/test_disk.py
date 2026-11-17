"""Disk usage overview: measuring, ledger attribution, cache, rendering. Never deletes."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import disk_usage  # noqa: E402
import ledger  # noqa: E402
import render  # noqa: E402


def fill(p: Path, n: int) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * n)


class DiskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "hermes"
        fill(self.home / "cache" / "scratch" / "big-task" / "node_modules" / "a.js", 5000)
        fill(self.home / "cache" / "scratch" / "big-task" / "b.txt", 1000)
        fill(self.home / "cache" / "scratch" / "small-task" / "c.txt", 100)
        fill(self.home / "cache" / "terminal" / "snap", 300)
        fill(self.home / "hermes-agent" / "x.py", 2000)
        fill(self.home / "config.yaml", 10)
        self.led_path = self.home / "plugin-shared" / "repo-status" / "ledger.db"
        self.led = ledger.Ledger(self.led_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_measure(self):
        before = sorted(str(p) for p in self.home.rglob("*"))
        d = disk_usage.measure(self.home)
        self.assertEqual([s["name"] for s in d["scratch"]], ["big-task", "small-task"])
        self.assertEqual(d["scratch"][0]["size"], 6000)
        self.assertEqual(d["scratch"][0]["files"], 2)
        self.assertEqual(d["scratch_size"], 6100)
        areas = {a["name"]: a for a in d["areas"]}
        self.assertEqual(areas["cache"]["size"], 6400)  # scratch + terminal
        self.assertEqual(areas["hermes-agent"]["size"], 2000)
        self.assertGreater(d["drive"]["total"], 0)
        self.assertEqual(before, sorted(str(p) for p in self.home.rglob("*")))  # nothing touched

    def test_attribution_from_ledger(self):
        target = self.home / "cache" / "scratch" / "big-task" / "b.txt"
        self.led.record(ledger.build_rows("write_file", {"path": str(target)}, "{}", cwd="C:\\REPOS\\proj", profile="d", session_id="s1", task_id=None))
        d = disk_usage.measure(self.home)
        disk_usage.attribute(d["scratch"], self.led_path)
        big = d["scratch"][0]
        self.assertEqual(big["sessions"][0]["id"], "s1")
        self.assertEqual(big["project_cwd"], "C:\\REPOS\\proj")
        self.assertNotIn("sessions", d["scratch"][1])

    def test_attribution_inside_a_parent_scratch_directory(self):
        nested = self.home / "cache" / "scratch" / "outer" / "home"
        target = nested / "cache" / "scratch" / "inner" / "file.txt"
        fill(target, 1)
        self.led.record(ledger.build_rows("write_file", {"path": str(target)}, "{}", cwd="C:\\REPOS\\proj",
                                          profile="d", session_id="nested-session", task_id=None))
        data = disk_usage.measure(nested)
        disk_usage.attribute(data["scratch"], self.led_path)
        self.assertEqual(data["scratch"][0]["sessions"][0]["id"], "nested-session")

    def test_refresh_cache_and_overview(self):
        data = disk_usage.refresh(self.home, self.led_path)
        self.assertTrue(disk_usage.cache_path(self.led_path).exists())
        ov = disk_usage.overview(self.home, self.led_path)
        self.assertTrue(ov["cached"])
        self.assertFalse(ov["scanning"])  # fresh cache: no background scan
        self.assertEqual(ov["scratch_size"], data["scratch_size"])
        text = render.render_disk(ov)
        self.assertIn("big-task", text)
        self.assertIn("deletes nothing", text)

    def test_overview_without_cache_starts_background_scan(self):
        ov = disk_usage.overview(self.home, self.led_path)
        self.assertFalse(ov["cached"])
        self.assertTrue(ov["scanning"])
        self.assertIn("computed in the background", render.render_disk(ov))
        deadline = time.time() + 20
        while time.time() < deadline and disk_usage.load_cached(self.led_path) is None:
            time.sleep(0.1)
        self.assertIsNotNone(disk_usage.load_cached(self.led_path))

    @unittest.skipUnless(os.name == "nt", "junctions are Windows-only")
    def test_junction_not_followed(self):
        import subprocess

        outside = Path(self.tmp.name) / "outside"
        fill(outside / "huge.bin", 50000)
        link = self.home / "cache" / "scratch" / "small-task" / "link"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        if not link.exists():
            self.skipTest("could not create junction")
        d = disk_usage.measure(self.home)
        small = next(s for s in d["scratch"] if s["name"] == "small-task")
        self.assertEqual(small["size"], 100)
        subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)


if __name__ == "__main__":
    unittest.main()

"""Hermes environment variables: core list, docs, secrets never returned."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import env_vars  # noqa: E402
import render  # noqa: E402

DOC = """# Environment Variables Reference
| Variable | Description |
|---|---|
| `HERMES_HOME` | Select the configuration and user-data home. |
| `HERMES_YOLO_MODE` | Set to `1` to bypass **dangerous-command** approval prompts. |
| `HERMES_CUSTOM_THING` | Something custom. |
"""


class EnvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        doc = self.home / "hermes-agent" / "website" / "docs" / "reference" / "environment-variables.md"
        doc.parent.mkdir(parents=True)
        doc.write_text(DOC, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_docs_parsed(self):
        docs = env_vars.load_docs(self.home)
        self.assertEqual(docs["HERMES_YOLO_MODE"], "Set to 1 to bypass dangerous-command approval prompts.")

    def test_snapshot(self):
        environ = {
            "HERMES_YOLO_MODE": "1",
            "HERMES_CUSTOM_THING": "abc",
            "TERMINAL_DOCKER_IMAGE": "img",
            "HERMES_DASHBOARD_SESSION_TOKEN": "TOPSECRET",
            "HERMES_SOME_API_KEY": "sk-abcdefghijklmnopqrstuvwxyz",
            "HERMES_WEIRD": "ghp_" + "a" * 30,
            "PATH": "x",
        }
        data = env_vars.snapshot(environ, self.home, resolved={"HERMES_HOME": "C:\\h", "HERMES_PROFILE": "default"})
        core = {c["name"]: c for c in data["core"]}
        self.assertEqual(core["HERMES_YOLO_MODE"]["value"], "1")
        self.assertTrue(core["HERMES_YOLO_MODE"]["set"])
        self.assertTrue(core["HERMES_YOLO_MODE"]["doc"].startswith("Set to 1"))
        self.assertEqual((core["HERMES_HOME"]["value"], core["HERMES_HOME"].get("resolved")), ("C:\\h", True))
        self.assertEqual(core["HERMES_MAX_ITERATIONS"]["default_code"], "500")
        other = {o["name"]: o for o in data["other"]}
        self.assertIn("TERMINAL_DOCKER_IMAGE", other)
        self.assertIn("HERMES_CUSTOM_THING", other)
        self.assertNotIn("PATH", other)
        self.assertEqual(data["hidden_secret_count"], 2)  # *_TOKEN, *_API_KEY
        self.assertIn("looks like", other["HERMES_WEIRD"]["value"])
        dumped = json.dumps(data)
        for secret in ("TOPSECRET", "sk-abcdefghijklmnopqrstuvwxyz", "ghp_"):
            self.assertNotIn(secret, dumped)
        text = render.render_env({**data, "process": "test", "chat": {"HERMES_SESSION_ID": "s1"}}, with_docs=True)
        self.assertIn("HERMES_YOLO_MODE", text)
        self.assertIn("Hidden secrets", text)
        self.assertNotIn("TOPSECRET", text)


if __name__ == "__main__":
    unittest.main()

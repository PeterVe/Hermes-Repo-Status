"""Honcho memory status: config without secrets, anonymous reachability, log events."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import honcho_status  # noqa: E402
import llm_log  # noqa: E402
import render  # noqa: E402

SECRET = "hch-SUPERSECRET-123456"


class _Auth401(BaseHTTPRequestHandler):
    seen_headers: list[dict] = []

    def do_GET(self):  # noqa: N802
        _Auth401.seen_headers.append(dict(self.headers))
        self.send_response(401)
        self.end_headers()

    def log_message(self, *a):
        pass


class HonchoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "plugins" / "honcho").mkdir(parents=True)
        (self.home / "logs").mkdir()
        (self.home / "honcho.json").write_text(json.dumps({
            "baseUrl": "https://honcho.example.invalid",
            "hosts": {"hermes": {"workspace": "alice", "peerName": "alice", "sessionStrategy": "per-repo",
                                 "apiKey": SECRET, "userPeerAliases": {"1": "alice", "2": "alice"}}},
        }), encoding="utf-8")
        self.config = {"memory": {"provider": "honcho", "memory_enabled": True}, "plugins": {"enabled": ["honcho"]}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_config_without_secrets(self):
        cfg = honcho_status.read_config(self.home, self.config)
        self.assertTrue(cfg["active"])
        self.assertTrue(cfg["plugin_enabled"])
        self.assertTrue(cfg["has_api_key"])
        self.assertEqual(cfg["alias_count"], 2)
        self.assertEqual(cfg["settings"]["sessionStrategy"], "per-repo")
        self.assertEqual(cfg["settings"]["baseUrl"], "https://honcho.example.invalid")
        self.assertNotIn(SECRET, json.dumps(cfg))
        self.assertNotIn(SECRET[-6:], json.dumps(cfg))

    def test_not_available(self):
        empty = Path(self.tmp.name) / "other"
        empty.mkdir()
        data = honcho_status.status(empty, {"memory": {"provider": None}})
        self.assertFalse(data["available"])
        self.assertIn("not available", render.render_honcho(data))

    def test_installed_but_other_provider(self):
        data = honcho_status.status(self.home, {"memory": {"provider": "mem0"}})
        self.assertTrue(data["available"])
        self.assertFalse(data["config"]["active"])
        self.assertIn("mem0", render.render_honcho(data))

    def test_connection_anonymous_401_is_reachable(self):
        srv = HTTPServer(("127.0.0.1", 0), _Auth401)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            res = honcho_status.check_connection(f"http://127.0.0.1:{srv.server_address[1]}")
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], 401)
        self.assertFalse(any("authorization" in {k.lower() for k in h} for h in _Auth401.seen_headers))

    def test_connection_failures(self):
        self.assertEqual(honcho_status.check_connection("http://127.0.0.1:9")["ok"], False)
        self.assertEqual(honcho_status.check_connection("https://nonexistent-host.invalid", timeout=3)["stage"], "dns")
        self.assertEqual(honcho_status.check_connection(None)["stage"], "config")

    def test_log_events(self):
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 30 * 86400))
        lines = [
            f"{now},100 INFO plugins.memory.honcho.session: Uploaded USER.md to Honcho for agent-main-discord-thread-1-1 (user peer)",
            f"{now},200 WARNING [x] plugins.memory.honcho.client: request failed api_key={SECRET}",
            f"{now},300 INFO agent.loop: unrelated line",
            f"{old},400 ERROR plugins.memory.honcho: too old",
        ]
        (self.home / "logs" / "agent.log").write_text("\n".join(lines), encoding="utf-8")
        (self.home / "logs" / "errors.log").write_text(lines[1], encoding="utf-8")  # duplicate warning
        data = honcho_status.status(self.home, self.config, days=7)
        logs = data["logs"]
        self.assertEqual(logs["counts"], {"INFO": 1, "WARNING": 1})
        self.assertEqual(logs["session_keys"][0]["key"], "agent-main-discord-thread-1-1")
        self.assertEqual(len(logs["problems"]), 1)
        self.assertNotIn(SECRET, json.dumps(logs))
        text = render.render_honcho(data)
        self.assertIn("per-repo", text)
        self.assertNotIn(SECRET, text)


class EndpointNormalizationTests(unittest.TestCase):
    def test_trailing_slash_dropped(self):
        self.assertEqual(llm_log.safe_base_url("https://chatgpt.com/backend-api/codex/"), "https://chatgpt.com/backend-api/codex")
        calls = [
            {"ts": 1, "status": "ok", "provider": "p", "model": "m", "base_url": "https://h/x/", "kind": "turn"},
            {"ts": 2, "status": "ok", "provider": "p", "model": "m", "base_url": "https://h/x", "kind": "turn"},
        ]
        self.assertEqual(llm_log.summarize_calls(calls)["providers"][0]["hosts"], ["https://h/x"])



if __name__ == "__main__":
    unittest.main()

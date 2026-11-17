"""F4: LLM provider call log — hook payloads as Hermes sends them, masking, summary."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import llm_log  # noqa: E402
import render  # noqa: E402


def pre(rid: str, **kw):
    return {"api_request_id": rid, "session_id": "s1", "task_id": "t1", "turn_id": "u1", "model": "gpt-x", "provider": "openai-codex",
            "base_url": "https://api.example.com/v1?key=SHOULD_NOT_STAY", "tool_count": 12, "approx_input_tokens": 4000, "max_tokens": 1000,
            "request": {"method": "POST", "body": {"messages": [{"role": "user", "content": "use token ghp_" + "a" * 36}], "api_key": "<redacted>"}}, **kw}


def post(rid: str, **kw):
    now = time.time()
    return {"api_request_id": rid, "session_id": "s1", "task_id": "t1", "turn_id": "u1", "platform": "desktop", "model": "gpt-x",
            "provider": "openai-codex", "base_url": "https://api.example.com/v1?key=SHOULD_NOT_STAY", "api_mode": "responses",
            "api_call_count": 1, "api_duration": 2.5, "started_at": now - 2.5, "ended_at": now, "first_chunk_at": now - 2.0,
            "finish_reason": "stop", "message_count": 7, "response_model": "gpt-x-2026",
            "response": {"model": "gpt-x-2026", "upstream_provider": "openai", "assistant_message": {"content": "hi Bearer abcdefghijklmnopqrstuvwxyz123"}},
            "usage": {"input_tokens": 1000, "output_tokens": 50, "cache_read_tokens": 3000, "cache_write_tokens": 0, "reasoning_tokens": 20},
            "assistant_content_chars": 2, "assistant_tool_call_count": 0, **kw}


class LlmLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = llm_log.LlmLog(Path(self.tmp.name) / "llm.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_turn_metadata_only_by_default(self):
        self.log.on_pre(pre("r1"), capture=False)
        cid = self.log.on_post(post("r1"), kind="turn", profile="default", capture=False)
        c = self.log.calls()[0]
        self.assertEqual((c["provider"], c["model"], c["response_model"], c["upstream_provider"]), ("openai-codex", "gpt-x", "gpt-x-2026", "openai"))
        self.assertEqual((c["input_tokens"], c["cache_read_tokens"], c["output_tokens"], c["reasoning_tokens"]), (1000, 3000, 50, 20))
        self.assertEqual((c["tool_count"], c["approx_input_tokens"], c["max_tokens"]), (12, 4000, 1000))  # from the pre hook
        self.assertAlmostEqual(c["ttfb"], 0.5, places=1)
        self.assertEqual(c["base_url"], "https://api.example.com/v1")  # query string dropped
        self.assertEqual(c["has_content"], 0)
        self.assertIsNone(self.log.content(cid)["request"])

    def test_capture_content_is_masked(self):
        self.log.on_pre(pre("r2"), capture=True)
        cid = self.log.on_post(post("r2"), kind="turn", profile="default", capture=True)
        item = self.log.content(cid)
        self.assertEqual(item["call"]["has_content"], 1)
        self.assertIn("<redacted>", item["request"])  # Hermes' own redaction preserved
        self.assertNotIn("ghp_" + "a" * 36, item["request"])
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123", item["response"])

    def test_error_hook(self):
        self.log.on_pre(pre("r3"), capture=False)
        self.log.on_error({"api_request_id": "r3", "session_id": "s1", "provider": "anthropic", "model": "claude", "status_code": 429,
                           "retry_count": 1, "api_duration": 0.3, "error": {"type": "RateLimitError", "message": "slow down sk-ant-" + "b" * 30}},
                          profile="default", capture=False)
        c = self.log.calls()[0]
        self.assertEqual((c["status"], c["status_code"], c["error_type"], c["tool_count"]), ("error", 429, "RateLimitError", 12))
        self.assertNotIn("b" * 30, c["error_message"])

    def test_aux_call(self):
        self.log.on_pre({"api_request_id": "a1", "aux_task": "title", "request_messages": [{"content": "RAW"}], "request": {"body": {}}}, capture=True)
        self.log.on_post(post("a1", aux_task="title", error=None), kind="aux", profile="default", capture=True)
        c = self.log.calls()[0]
        self.assertEqual((c["kind"], c["aux_task"]), ("aux", "title"))
        item = self.log.content(c["id"])
        self.assertNotIn("RAW", item["request"] or "")  # unsanitized request_messages never stored

    def test_aux_error(self):
        self.log.on_post(post("a2", aux_task="compression", error="TimeoutError: x", error_type="TimeoutError", usage=None, response=None),
                         kind="aux", profile="default", capture=False)
        self.assertEqual(self.log.calls()[0]["status"], "error")

    def test_summary_and_render(self):
        for i in range(3):
            self.log.on_post(post(f"s{i}"), kind="turn", profile="default", capture=False)
        self.log.on_post(post("g1", provider="antigravity-oauth", model="gemini"), kind="turn", profile="default", capture=False)
        self.log.on_error({"api_request_id": "e1", "provider": "anthropic", "model": "claude", "status_code": 500, "error": {"type": "X", "message": "boom"}}, profile="d", capture=False)
        data = llm_log.summarize_calls(self.log.calls())
        self.assertEqual(data["total"]["calls"], 5)
        self.assertEqual(data["total"]["errors"], 1)
        provs = {p["provider"]: p for p in data["providers"]}
        self.assertEqual(provs["openai-codex"]["calls"], 3)
        self.assertEqual(provs["openai-codex"]["input_tokens"], 3000)
        self.assertEqual(data["sessions"][0]["session_id"], "s1")
        data["enabled"], data["capture_content"] = True, False
        text = render.render_llm(data, label_text="test")
        self.assertIn("openai-codex", text)
        self.assertIn("boom", text)
        self.assertIn("not stored", text)

    def test_content_retention(self):
        log = llm_log.LlmLog(Path(self.tmp.name) / "r.db", content_retention_days=1)
        log.on_pre(pre("old"), capture=True)
        log.on_post(post("old", started_at=time.time() - 3 * 86400), kind="turn", profile="d", capture=True)
        log._last_prune = 0
        log.on_post(post("new"), kind="turn", profile="d", capture=False)
        rows = {r["api_request_id"]: r for r in log.calls()}
        self.assertEqual(rows["old"]["has_content"], 0)  # content expired, metadata kept
        self.assertIn("new", rows)

    def test_pending_is_bounded(self):
        for i in range(llm_log.PENDING_MAX + 50):
            self.log.on_pre(pre(f"p{i}"), capture=False)
        self.assertLessEqual(len(self.log._pending), llm_log.PENDING_MAX)


class HookWiringTests(unittest.TestCase):
    def test_hooks_registered_and_settings_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = importlib.util.spec_from_file_location("rs_pkg_llm", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
            pkg = importlib.util.module_from_spec(spec)
            sys.modules["rs_pkg_llm"] = pkg
            spec.loader.exec_module(pkg)
            pkg._llm = pkg.llm_log.LlmLog(Path(tmp) / "llm.db")
            config = {}

            class Ctx:
                profile_name = "default"
                hooks, tools, cmds = {}, {}, {}

                def get_config(self, key, default=None):
                    return config.get(key, default)

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
            for name in ("pre_api_request", "post_api_request", "api_request_error", "pre_auxiliary_call", "post_auxiliary_call"):
                self.assertIn(name, ctx.hooks)
            ctx.hooks["pre_api_request"](**pre("w1"))
            ctx.hooks["post_api_request"](**post("w1"), assistant_message=object())
            self.assertEqual(len(pkg._llm.calls()), 1)
            ctx.hooks["post_api_request"](**{"broken": True})  # must not raise
            config["llm_log"] = False
            ctx.hooks["post_api_request"](**post("w2"))
            self.assertEqual(len([c for c in pkg._llm.calls() if c["api_request_id"] == "w2"]), 0)
            self.assertIn("Recording is off", ctx.cmds["repo"]("llm 24h"))
            config["llm_log"] = True
            self.assertIn("openai-codex", ctx.cmds["repo"]("llm 24h"))
            sys.modules.pop("rs_pkg_llm", None)


if __name__ == "__main__":
    unittest.main()

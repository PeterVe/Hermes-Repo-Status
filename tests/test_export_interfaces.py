"""Export entry points never bypass the shared exporter or its disabled setting."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent


def load_plugin():
    name = "repo_status_export_interfaces"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ExportInterfaceTests(unittest.TestCase):
    def test_settings_disable_export_by_default(self):
        plugin = load_plugin()
        ctx = Mock()
        ctx.get_config.side_effect = lambda key, default=None: default
        settings = plugin._settings(ctx)
        self.assertEqual(settings.get("export_dir"), "")
        self.assertEqual(settings.get("export_include_secrets"), False)
        self.assertEqual(settings.get("export_include_scratch"), False)
        self.assertEqual(settings.get("export_scratch_cap_mb"), 500)
        self.assertEqual(settings.get("export_keep_last"), 0)

    def test_cli_accepts_export_dry_run(self):
        parser = argparse.ArgumentParser()
        load_plugin()._setup_cli(parser)
        args = parser.parse_args(["--export", "--dry-run"])
        self.assertTrue(args.export)
        self.assertTrue(args.dry_run)

    def test_slash_export_status_uses_shared_job(self):
        plugin = load_plugin()
        with patch.object(plugin, "export", new=Mock()) as exporter:
            exporter.status.return_value = {"state": "running", "files": 2, "estimated_bytes": 10}
            result = plugin._make_slash_handler(Mock())("export status")
        exporter.status.assert_called_once_with()
        self.assertIn("2", result)

    def test_backend_export_routes(self):
        spec = importlib.util.spec_from_file_location("export_test_api", ROOT / "dashboard" / "plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        paths = {route.path for route in api.router.routes}
        self.assertTrue({"/export", "/export/status", "/export/list"} <= paths)
        with patch.object(api, "_settings", return_value={"export_dir": ""}):
            with patch.object(api, "_exporter") as loader:
                loader.return_value.status.return_value = {"state": "idle"}
                result = asyncio.run(api.export_status())
                self.assertFalse(result["enabled"])

    def test_export_summary_reports_warnings(self):
        text = load_plugin()._export_text({"ok": True, "state": "dry_run", "warnings": ["Uncheckpointed WAL omitted"]})
        self.assertIn("Uncheckpointed WAL omitted", text)

    def test_export_progress_translates_step(self):
        plugin = load_plugin()
        text = plugin._export_text({"ok": True, "state": "running", "step": "collect", "files": 2})
        self.assertIn(plugin.tr("export.step.collect"), text)

    def test_export_error_reports_recovery_directory(self):
        text = load_plugin()._export_text({"ok": False, "state": "error", "error": "CRC mismatch",
                                           "work_dir": "C:/exports/_work/project_2026-11-17_1600"})
        self.assertIn("CRC mismatch", text)
        self.assertIn("C:/exports/_work/project_2026-11-17_1600", text)

    def test_export_summary_translates_state(self):
        plugin = load_plugin()
        text = plugin._export_text({"ok": True, "state": "dry_run", "files": 2})
        self.assertIn(plugin.tr("export.state.dry_run"), text)

    def test_completed_export_reports_archive_path(self):
        text = load_plugin()._export_text({"ok": True, "state": "completed", "archive": "C:/exports/project.zip"})
        self.assertIn("C:/exports/project.zip", text)

    def test_cli_export_runs_in_foreground(self):
        plugin = load_plugin()
        ctx = Mock()
        ctx.get_config.side_effect = lambda key, default=None: default
        parser = argparse.ArgumentParser()
        plugin._setup_cli(parser)
        args = parser.parse_args(["--export", "--dry-run", "--json"])
        with patch.object(plugin, "export", new=Mock()) as exporter, patch("builtins.print"):
            exporter.start.return_value = {"ok": True, "state": "dry_run", "files": 1}
            result = plugin._make_cli_handler(ctx)(args)
        self.assertEqual(result, 0)
        self.assertFalse(exporter.start.call_args.kwargs["background"])
        self.assertTrue(exporter.start.call_args.kwargs["dry_run"])


if __name__ == "__main__":
    unittest.main()

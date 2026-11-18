"""The panel, the backend and the dashboard manifest report one version; /version sees stale copies."""
from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def yaml_version() -> str:
    return re.search(r"^version:\s*(\S+)", (ROOT / "plugin.yaml").read_text(encoding="utf-8"), re.M).group(1)


def load_api():
    spec = importlib.util.spec_from_file_location("version_test_api", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api


class VersionTests(unittest.TestCase):
    def test_panel_manifest_and_plugin_versions_match(self):
        panel = re.search(r"const PANEL_VERSION = '([^']+)'", (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")).group(1)
        manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))["version"]
        self.assertEqual(panel, yaml_version())
        self.assertEqual(manifest, yaml_version())

    def test_version_route_and_profile_copies(self):
        try:
            api = load_api()
        except ModuleNotFoundError as exc:  # fastapi lives only in the Hermes agent venv
            self.skipTest(str(exc))
        self.assertIn("/version", {route.path for route in api.router.routes})
        self.assertEqual(api.version()["version"], yaml_version())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel, ver in (("plugins/repo-status", "0.6.1"), ("profiles/work/plugins/repo-status", "0.5.0")):
                (root / rel).mkdir(parents=True)
                (root / rel / "plugin.yaml").write_text(f"name: repo-status\nversion: {ver}\n", encoding="utf-8")
            (root / "profiles" / "empty").mkdir()
            self.assertEqual(api.plugin_copies(root), [{"profile": "default", "version": "0.6.1"}, {"profile": "work", "version": "0.5.0"}])


if __name__ == "__main__":
    unittest.main()

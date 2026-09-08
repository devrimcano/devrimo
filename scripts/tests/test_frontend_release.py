import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

spec = importlib.util.spec_from_file_location("frontend_release", Path(__file__).parents[1] / "frontend_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def build(path):
    files = {"BUILD_ID": "synthetic", "routes-manifest.json": "{}", "server/pages/500.html": "error",
             "server/app-paths-manifest.json": json.dumps({"/admin/page": "app/admin/page.js",
                                                         "/login/page": "app/login/page.js"})}
    for page in ("admin", "login"):
        files[f"server/app/{page}/page.js"] = "module"
        files[f"server/app/{page}/page_client-reference-manifest.js"] = "manifest"
    for name, data in files.items():
        target = path / ".next" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data)


class FrontendReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_missing_admin_manifest_blocks_release(self):
        build(self.root)
        release.validate_build(self.root)
        (self.root / ".next/server/app/admin/page_client-reference-manifest.js").unlink()
        with self.assertRaisesRegex(RuntimeError, "admin.*manifest"):
            release.validate_build(self.root)

    def test_missing_error_page_blocks_release(self):
        build(self.root)
        (self.root / ".next/server/pages/500.html").unlink()
        with self.assertRaisesRegex(RuntimeError, "500.html"):
            release.validate_build(self.root)

    def test_failed_build_never_changes_serving_tree(self):
        current = self.root / "current"
        build(current)
        (current / ".env.local").write_text("SYNTHETIC=test")
        (current / "package.json").write_text("{}")
        before = {str(p.relative_to(current)): p.read_bytes() for p in current.rglob("*") if p.is_file()}
        with (
            patch.object(release.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "npm")),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            release.prepare(current, self.root / "new", current / ".env.local", Path("/bin"), "abc")
        after = {str(p.relative_to(current)): p.read_bytes() for p in current.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_failed_switch_preserves_current_release(self):
        old, new, current = self.root / "old", self.root / "new", self.root / "current"
        build(old)
        build(new)
        current.symlink_to(old)
        with (
            patch.object(release.os, "replace", side_effect=OSError("synthetic switch failure")),
            self.assertRaises(OSError),
        ):
            release.switch(current, new)
        self.assertEqual(current.resolve(), old)
        self.assertFalse((self.root / "current.switch").exists())

    def test_switch_retains_old_build_and_supports_rollback(self):
        old, new, current = self.root / "old", self.root / "new", self.root / "current"
        build(old)
        build(new)
        current.symlink_to(old)
        release.switch(current, new)
        self.assertEqual(current.resolve(), new)
        release.validate_build(old)
        release.switch(current, old)
        self.assertEqual(current.resolve(), old)

    def test_rejects_build_in_existing_directory(self):
        build(self.root)
        with self.assertRaisesRegex(RuntimeError, "new directory"):
            release.prepare(self.root, self.root, self.root / ".env.local", Path("/bin"), "abc")

    def test_healthy_login_with_missing_asset_fails_health_check(self):
        response = io.BytesIO(b'<html><script src="/_next/static/missing.js"></script></html>')
        response.status = 200
        response.headers = {"Content-Type": "text/html"}
        with (
            patch.object(release, "urlopen", side_effect=[response, HTTPError("asset", 404, "missing", {}, None)]),
            self.assertRaises(HTTPError),
        ):
            release.check_http("http://127.0.0.1:3000")


if __name__ == "__main__":
    unittest.main()

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

    def test_release_is_started_the_way_that_release_can_be_started(self):
        """The unit runs one launcher for two shapes of release; this is the choice it makes."""
        standalone, legacy = self.root / "standalone", self.root / "legacy"
        standalone.mkdir()
        (standalone / "server.js").write_text("// the server itself")
        (legacy / "node_modules/next/dist/bin").mkdir(parents=True)
        (legacy / "node_modules/next/dist/bin/next").write_text("// the CLI")

        self.assertEqual(release.server_command(standalone, "node", "127.0.0.1", 3100),
                         ["node", str(standalone / "server.js")])
        # A standalone build has no --port to give; PORT and HOSTNAME carry it.
        self.assertNotIn("--port", release.server_command(standalone, "node", "127.0.0.1", 3100))
        self.assertIn("--port", release.server_command(legacy, "node", "127.0.0.1", 3100))

    def test_prebuilt_release_is_installed_rather_than_built(self):
        prebuilt, target = self.root / "prebuilt", self.root / "target"
        prebuilt.mkdir()
        build(prebuilt)
        (prebuilt / "server.js").write_text("// the server itself")
        (prebuilt / ".env.local").write_text("LEAKED=from the builder")
        env_file = self.root / "host.env"
        env_file.write_text("NEXT_PUBLIC_SITE_URL=https://devrimo.com")

        with (
            patch.object(release, "smoke") as smoked,
            patch.object(release, "subprocess") as ran,
            # Deploys run as root and chown the release to the runtime owner;
            # this test is about what gets copied, and runs anywhere.
            patch.object(release.os, "geteuid", create=True, return_value=1000),
        ):
            release.prepare(self.root / "source", target, env_file, Path("/bin"), "abc", prebuilt)

        smoked.assert_called_once()
        # Nothing was built here: that is the entire point of the path.
        ran.run.assert_not_called()
        self.assertTrue((target / "server.js").is_file())
        # The host's own configuration wins; the builder's copy never travels.
        self.assertEqual((target / ".env.local").read_text(), "NEXT_PUBLIC_SITE_URL=https://devrimo.com")
        self.assertEqual((target / ".release-sha").read_text().strip(), "abc")

    def test_prebuilt_without_a_server_is_refused(self):
        prebuilt = self.root / "prebuilt"
        prebuilt.mkdir()
        build(prebuilt)
        env_file = self.root / "host.env"
        env_file.write_text("")
        with self.assertRaisesRegex(RuntimeError, "standalone"):
            release.prepare(self.root / "source", self.root / "target", env_file, Path("/bin"), "abc", prebuilt)

    def test_the_outgoing_builds_assets_survive_the_switch(self):
        """A student with the app open is still asking for the old filenames."""
        previous, incoming = self.root / "previous", self.root / "incoming"
        for path, chunk, content in (
            (previous, "old-only.js", "outgoing"),
            (previous, "shared.js", "outgoing version"),
            (incoming, "new-only.js", "incoming"),
            (incoming, "shared.js", "incoming version"),
        ):
            target = path / ".next/static/chunks" / chunk
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

        carried = release.carry_static_forward(previous, incoming)

        chunks = incoming / ".next/static/chunks"
        self.assertEqual(carried, 1)
        # The outgoing build's own chunk is reachable again.
        self.assertEqual((chunks / "old-only.js").read_text(), "outgoing")
        self.assertEqual((chunks / "new-only.js").read_text(), "incoming")
        # Where both have a file the incoming build wins: the names carry a
        # content hash, so this only happens for files that are not hashed.
        self.assertEqual((chunks / "shared.js").read_text(), "incoming version")

    def test_carrying_assets_is_skipped_when_there_is_nothing_to_carry(self):
        incoming = self.root / "incoming"
        (incoming / ".next/static").mkdir(parents=True)
        self.assertEqual(release.carry_static_forward(self.root / "nothing-here", incoming), 0)

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

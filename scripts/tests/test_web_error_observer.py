import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("web_error_observer", Path(__file__).parents[1] / "web_error_observer.py")
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


class WebObserverTests(unittest.TestCase):
    def test_framework_failure_is_classified_without_content(self):
        record = {"__CURSOR": "test-cursor", "__REALTIME_TIMESTAMP": "1788880920000000",
                  "MESSAGE": 'The client reference manifest for route "/private-user-content" does not exist.'}
        category, description = observer.classify(record["MESSAGE"])
        event = observer.payload(record, category, description, "synthetic", release_sha="abc123")
        self.assertEqual(category, "missing_client_manifest")
        self.assertNotIn("private-user-content", str(event))
        self.assertEqual(event["uuid"], observer.payload(record, category, description, "synthetic", release_sha="abc123")["uuid"])
        self.assertEqual(event["properties"]["release"], "abc123")

    def test_release_environment_is_used_by_the_standalone_observer(self):
        with patch.dict(os.environ, {"RELEASE_SHA": "release-from-systemd"}, clear=False):
            self.assertEqual(observer.deployed_release(), "release-from-systemd")

    def test_unrelated_output_is_not_forwarded(self):
        self.assertIsNone(observer.classify("a private conversation or an ordinary request"))

    def test_missing_error_page_is_reported(self):
        self.assertEqual(observer.classify("Failed to load static file for page: /500 ENOENT")[0],
                         "missing_error_page")


if __name__ == "__main__":
    unittest.main()

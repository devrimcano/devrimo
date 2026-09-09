import importlib.util
import stat
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("set_runtime_env", Path(__file__).parents[1] / "set_runtime_env.py")
runtime_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime_env)


class RuntimeEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "api.env"

    def test_replaces_value_once_without_changing_other_settings_or_mode(self):
        self.path.write_text("DATABASE_URL=secret\nACADEMIC_CATALOG_INGESTION_ENABLED=false\n")
        self.path.chmod(0o600)
        runtime_env.set_value(self.path, "ACADEMIC_CATALOG_INGESTION_ENABLED", "true")
        self.assertEqual(
            self.path.read_text(),
            "DATABASE_URL=secret\nACADEMIC_CATALOG_INGESTION_ENABLED=true\n",
        )
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_appends_missing_value_and_removes_duplicate_assignments(self):
        self.path.write_text("DATABASE_URL=secret\nACADEMIC_CATALOG_INGESTION_ENABLED=false\n"
                             "ACADEMIC_CATALOG_INGESTION_ENABLED=old\n")
        runtime_env.set_value(self.path, "ACADEMIC_CATALOG_INGESTION_ENABLED", "true")
        self.assertEqual(self.path.read_text().count("ACADEMIC_CATALOG_INGESTION_ENABLED="), 1)
        self.assertTrue(self.path.read_text().endswith("ACADEMIC_CATALOG_INGESTION_ENABLED=true\n"))

    def test_rejects_non_regular_files_and_multiline_values(self):
        with self.assertRaisesRegex(ValueError, "regular file"):
            runtime_env.set_value(self.path, "ACADEMIC_CATALOG_INGESTION_ENABLED", "true")
        self.path.write_text("")
        with self.assertRaisesRegex(ValueError, "one line"):
            runtime_env.set_value(self.path, "ACADEMIC_CATALOG_INGESTION_ENABLED", "true\nBAD=true")


if __name__ == "__main__":
    unittest.main()

import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "check_migration_graph", Path(__file__).parents[1] / "check_migration_graph.py"
)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def revision_file(directory: Path, name: str, revision: str, down: str | None) -> None:
    parent = f'"{down}"' if down else "None"
    (directory / name).write_text(
        f'"""A revision."""\n\nrevision = "{revision}"\ndown_revision = {parent}\n',
        encoding="utf-8",
    )


class MigrationGraphTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def check(self):
        return checker.problems(checker.read_revisions(self.directory))

    def test_a_linear_chain_is_accepted(self):
        revision_file(self.directory, "0001_start.py", "0001_start", None)
        revision_file(self.directory, "0002_next.py", "0002_next", "0001_start")
        self.assertEqual(self.check(), [])

    def test_two_revisions_on_one_parent_are_reported_as_two_heads(self):
        """The mistake: two people number the next migration at the same time."""
        revision_file(self.directory, "0001_start.py", "0001_start", None)
        revision_file(self.directory, "0002_catalog.py", "0002_catalog", "0001_start")
        revision_file(self.directory, "0002_headroom.py", "0002_headroom", "0001_start")
        reported = " ".join(self.check())
        self.assertIn("two heads", reported)
        self.assertIn("0002_catalog.py", reported)
        self.assertIn("0002_headroom.py", reported)
        self.assertIn("numbered 0002", reported)

    def test_a_revision_id_too_long_for_the_version_column_is_reported(self):
        """The mistake: a descriptive id that alembic_version cannot store."""
        long_id = "0002_api_restart_connection_headroom"
        self.assertGreater(len(long_id), checker.VERSION_NUM_LIMIT)
        revision_file(self.directory, "0001_start.py", "0001_start", None)
        revision_file(self.directory, "0002_long.py", long_id, "0001_start")
        reported = " ".join(self.check())
        self.assertIn("32", reported)
        self.assertIn(long_id, reported)

    def test_a_down_revision_pointing_nowhere_is_reported(self):
        revision_file(self.directory, "0001_start.py", "0001_start", None)
        revision_file(self.directory, "0002_next.py", "0002_next", "0001_typo")
        self.assertIn("does not exist", " ".join(self.check()))

    def test_two_roots_are_reported(self):
        revision_file(self.directory, "0001_start.py", "0001_start", None)
        revision_file(self.directory, "0001_other.py", "0001_other", None)
        self.assertIn("no down_revision", " ".join(self.check()))

    def test_the_repository_graph_is_clean(self):
        versions = Path(__file__).resolve().parents[2] / "backend/alembic/versions"
        revisions = checker.read_revisions(versions)
        self.assertTrue(revisions, f"no revisions found in {versions}")
        self.assertEqual(checker.problems(revisions), [])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.models.db import resolve_db_path
from src.pipeline import FeedbackPipeline
from src.utils.config import AppConfig


class TestDatabaseUrlResolution(unittest.TestCase):
    """
    Regression guard for a real bug: FeedbackPipeline used to call init_db() with no
    argument, so config.database_url was dead configuration and any pipeline built
    with an in-memory URL still wrote into the developer's data/feedbacks.db.
    """

    def test_relative_sqlite_url(self):
        self.assertEqual(str(resolve_db_path("sqlite:///data/x.db")), "data/x.db")

    def test_absolute_sqlite_url_with_four_slashes(self):
        self.assertEqual(str(resolve_db_path("sqlite:////tmp/x.db")), "/tmp/x.db")

    def test_memory_url(self):
        self.assertEqual(str(resolve_db_path("sqlite:///:memory:")), ":memory:")

    def test_bare_path_is_used_verbatim(self):
        self.assertEqual(str(resolve_db_path("/var/data/x.db")), "/var/data/x.db")

    def test_non_sqlite_scheme_is_rejected_loudly(self):
        with self.assertRaises(ValueError):
            resolve_db_path("postgresql://localhost/db")

    def test_pipeline_honours_in_memory_config(self):
        """The pipeline must not touch the real data/ directory when told not to."""
        pipeline = FeedbackPipeline(AppConfig(database_url="sqlite:///:memory:"))
        try:
            self.assertEqual(str(pipeline.db_path), ":memory:")
            # Schema must exist on the connection the pipeline actually holds.
            tables = {
                row[0]
                for row in pipeline.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("feedbacks", tables)
            self.assertIn("checkpoints", tables)
        finally:
            pipeline.close()

    def test_pipeline_writes_to_the_configured_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "custom.db"
            pipeline = FeedbackPipeline(AppConfig(database_url=f"sqlite:///{target}"))
            try:
                self.assertTrue(target.exists(), "configured database file was not created")
            finally:
                pipeline.close()


if __name__ == "__main__":
    unittest.main()

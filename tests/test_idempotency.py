from __future__ import annotations

import sqlite3
import unittest
from src.models.db import get_db_connection, init_db
from src.models.schema import FeedbackItem
from src.storage.repository import FeedbackRepository


class TestIdempotency(unittest.TestCase):
    """
    Acceptance Criteria Test:
    '连续运行 3 次，数据库无重复记录（幂等验证）'
    """

    def setUp(self):
        # Use an isolated in-memory SQLite database
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")

        # Initialize schema directly on memory connection
        init_db(db_path=":memory:")
        # Re-create tables on self.conn
        with self.conn:
            self.conn.execute("""
                CREATE TABLE feedbacks (
                    id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    content TEXT NOT NULL,
                    author TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metrics TEXT NOT NULL,
                    url TEXT NOT NULL,
                    source_target TEXT NOT NULL,
                    raw_data TEXT NOT NULL,
                    collected_at TEXT NOT NULL
                );
            """)
        self.repo = FeedbackRepository(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_repeated_insert_is_idempotent(self):
        item = FeedbackItem(
            id="discord:999:888",
            platform="discord",
            content="Sample recurring bug report.",
            author="developer_1",
            created_at="2026-10-13T12:00:00Z",
            metrics={"likes": 3},
            url="https://discord.com/channels/999/888/777",
            source_target="999",
        )

        # Run 1: First insert should succeed
        inserted_1 = self.repo.save_feedback(item)
        self.assertTrue(inserted_1, "First insert must return True")

        # Run 2: Duplicate insert should be ignored
        inserted_2 = self.repo.save_feedback(item)
        self.assertFalse(inserted_2, "Second insert must return False (ignored duplicate)")

        # Run 3: Third insert should also be ignored
        inserted_3 = self.repo.save_feedback(item)
        self.assertFalse(inserted_3, "Third insert must return False")

        # Verify DB has exactly 1 row
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) AS total FROM feedbacks WHERE id = ?", (item.id,))
        count = cursor.fetchone()["total"]
        self.assertEqual(count, 1, "There must be exactly 1 record in database despite 3 runs.")


if __name__ == "__main__":
    unittest.main()

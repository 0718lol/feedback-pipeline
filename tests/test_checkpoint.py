from __future__ import annotations

import sqlite3
import unittest
from src.storage.checkpoint import CheckpointManager


class TestCheckpointAndIncremental(unittest.TestCase):
    """
    Acceptance Criteria Test:
    '断点续传：程序中断后重启能从断点继续'
    '增量同步：记录每条内容唯一 ID 和发布时间，每次运行只采新增内容'
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute("""
                CREATE TABLE checkpoints (
                    checkpoint_key TEXT PRIMARY KEY,
                    last_item_id TEXT,
                    last_timestamp TEXT,
                    items_count INTEGER DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
            """)
        self.checkpoint_mgr = CheckpointManager(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_checkpoint_lifecycle_and_resume(self):
        key = "discord:channel_123456"

        # 1. Initial state has no checkpoint
        item_id, timestamp, count = self.checkpoint_mgr.get_checkpoint(key)
        self.assertIsNone(item_id)
        self.assertIsNone(timestamp)
        self.assertEqual(count, 0)

        # 2. Update watermark after first batch
        self.checkpoint_mgr.update_checkpoint(
            checkpoint_key=key,
            last_item_id="msg_1001",
            last_timestamp="2026-10-13T10:00:00Z",
            increment_count=15,
        )

        # 3. Simulate program crash / restart -> fetch checkpoint
        resumed_id, resumed_ts, resumed_count = self.checkpoint_mgr.get_checkpoint(key)
        self.assertEqual(resumed_id, "msg_1001")
        self.assertEqual(resumed_ts, "2026-10-13T10:00:00Z")
        self.assertEqual(resumed_count, 15)

        # 4. Next run captures newer batch and updates watermark
        self.checkpoint_mgr.update_checkpoint(
            checkpoint_key=key,
            last_item_id="msg_1005",
            last_timestamp="2026-10-13T11:00:00Z",
            increment_count=4,
        )

        final_id, _, final_count = self.checkpoint_mgr.get_checkpoint(key)
        self.assertEqual(final_id, "msg_1005")
        self.assertEqual(final_count, 19)


if __name__ == "__main__":
    unittest.main()

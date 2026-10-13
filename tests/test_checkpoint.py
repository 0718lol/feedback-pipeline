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


class TestSeenIdSet(unittest.TestCase):
    """
    The seen-set is what makes incremental sync sound for relevance-ordered feeds
    such as Xiaohongshu search results, where the newest id is not a usable cursor.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE checkpoints (
                    checkpoint_key TEXT PRIMARY KEY,
                    last_item_id TEXT,
                    last_timestamp TEXT,
                    items_count INTEGER DEFAULT 0,
                    seen_ids TEXT DEFAULT '[]',
                    updated_at TEXT NOT NULL
                );
                """
            )
        self.checkpoint_mgr = CheckpointManager(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_empty_by_default(self):
        self.assertEqual(self.checkpoint_mgr.get_seen_ids("xhs:AI工具"), set())

    def test_ids_accumulate_across_calls(self):
        self.checkpoint_mgr.add_seen_ids("xhs:AI工具", ["a", "b"])
        self.checkpoint_mgr.add_seen_ids("xhs:AI工具", ["b", "c"])
        self.assertEqual(self.checkpoint_mgr.get_seen_ids("xhs:AI工具"), {"a", "b", "c"})

    def test_adding_is_idempotent(self):
        self.checkpoint_mgr.add_seen_ids("xhs:AI工具", ["a"])
        first = self.checkpoint_mgr.add_seen_ids("xhs:AI工具", ["a"])
        second = self.checkpoint_mgr.add_seen_ids("xhs:AI工具", ["a"])
        self.assertEqual(first, 1)
        self.assertEqual(second, 1)

    def test_seen_ids_are_scoped_per_target(self):
        self.checkpoint_mgr.add_seen_ids("xhs:AI工具", ["a"])
        self.checkpoint_mgr.add_seen_ids("xhs:数据科学", ["b"])
        self.assertEqual(self.checkpoint_mgr.get_seen_ids("xhs:AI工具"), {"a"})
        self.assertEqual(self.checkpoint_mgr.get_seen_ids("xhs:数据科学"), {"b"})

    def test_empty_input_is_a_noop(self):
        self.checkpoint_mgr.add_seen_ids("xhs:AI工具", [])
        self.assertEqual(self.checkpoint_mgr.get_seen_ids("xhs:AI工具"), set())

    def test_corrupt_payload_degrades_to_empty_set(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO checkpoints (checkpoint_key, seen_ids, updated_at) VALUES (?,?,?)",
                ("xhs:broken", "not-json", "2026-10-13T00:00:00Z"),
            )
        self.assertEqual(self.checkpoint_mgr.get_seen_ids("xhs:broken"), set())

    def test_reset_clears_both_watermark_and_seen_set(self):
        self.checkpoint_mgr.update_checkpoint("xhs:AI工具", last_item_id="a")
        self.checkpoint_mgr.add_seen_ids("xhs:AI工具", ["a"])
        self.checkpoint_mgr.reset_checkpoint("xhs:AI工具")
        self.assertIsNone(self.checkpoint_mgr.get_checkpoint("xhs:AI工具")[0])
        self.assertEqual(self.checkpoint_mgr.get_seen_ids("xhs:AI工具"), set())


if __name__ == "__main__":
    unittest.main()

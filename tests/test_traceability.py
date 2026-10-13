from __future__ import annotations

import sqlite3
import unittest
from src.models.db import init_db
from src.models.schema import AnalysisResult, FeedbackItem
from src.storage.repository import FeedbackRepository


class TestTraceability(unittest.TestCase):
    """
    Acceptance Criteria Test:
    '随机抽 10 条分析结果，都能通过原始链接找到原帖（可追溯验证）'
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
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
            self.conn.execute("""
                CREATE TABLE analysis_results (
                    feedback_id TEXT PRIMARY KEY,
                    sentiment TEXT NOT NULL,
                    category TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    model TEXT NOT NULL,
                    analyzed_at TEXT NOT NULL
                );
            """)
        self.repo = FeedbackRepository(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_trace_link_from_analysis_to_original_post(self):
        # Insert 10 sample items
        for i in range(1, 11):
            f_id = f"discord:test_ch:{1000 + i}"
            target_url = f"https://discord.com/channels/123/456/{1000 + i}"

            item = FeedbackItem(
                id=f_id,
                platform="discord",
                content=f"Feedback issue {i}",
                author=f"user_{i}",
                created_at="2026-10-13T12:00:00Z",
                url=target_url,
            )
            self.repo.save_feedback(item)

            analysis = AnalysisResult(
                feedback_id=f_id,
                sentiment="negative",
                category="bug",
                summary=f"Summary {i}",
                confidence=0.9,
                model="test-llm",
            )
            self.repo.save_analysis(analysis)

        # Audit 10 items
        for i in range(1, 11):
            f_id = f"discord:test_ch:{1000 + i}"
            trace_record = self.repo.get_traceable_feedback(f_id)
            self.assertIsNotNone(trace_record)
            self.assertEqual(trace_record["url"], f"https://discord.com/channels/123/456/{1000 + i}")
            self.assertTrue(trace_record["url"].startswith("https://"))
            self.assertEqual(trace_record["sentiment"], "negative")


if __name__ == "__main__":
    unittest.main()

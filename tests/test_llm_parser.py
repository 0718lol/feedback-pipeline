from __future__ import annotations

import json
import sqlite3
import unittest
from src.analyzer.llm_analyzer import LLMAnalyzer
from src.models.schema import FeedbackItem
from src.storage.repository import FeedbackRepository
from src.utils.config import AppConfig


class TestLLMParser(unittest.TestCase):
    """
    Test LLM response cleaning, parsing, and structured extraction.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.repo = FeedbackRepository(self.conn)
        self.config = AppConfig(openai_api_key="")  # Offline mode
        self.analyzer = LLMAnalyzer(self.config, self.repo)

    def tearDown(self):
        self.conn.close()

    def test_clean_json_markdown_fences(self):
        fenced_input = """```json
{
  "sentiment": "negative",
  "category": "bug",
  "summary": "Data export failed",
  "confidence": 0.95
}
```"""
        cleaned = self.analyzer._clean_json_response(fenced_input)
        parsed = json.loads(cleaned)
        self.assertEqual(parsed["sentiment"], "negative")
        self.assertEqual(parsed["category"], "bug")

    def test_clean_json_with_extra_text(self):
        extra_text_input = """Here is the structured analysis requested:
{
  "sentiment": "positive",
  "category": "praise",
  "summary": "Great tool speed",
  "confidence": 0.88
}
Hope this helps!"""
        cleaned = self.analyzer._clean_json_response(extra_text_input)
        parsed = json.loads(cleaned)
        self.assertEqual(parsed["sentiment"], "positive")
        self.assertEqual(parsed["category"], "praise")

    def test_heuristic_offline_fallback(self):
        item = FeedbackItem(
            id="discord:test:1",
            platform="discord",
            content="Critical bug: System crashed with memory out of bounds exception!",
            author="tester",
            created_at="2026-10-13T12:00:00Z",
            metrics={},
            url="https://discord.com/channels/1/2/3",
        )
        result = self.analyzer.analyze_item(item)
        self.assertEqual(result.sentiment, "negative")
        self.assertEqual(result.category, "bug")
        self.assertEqual(result.feedback_id, item.id)
        self.assertGreaterEqual(result.confidence, 0.0)
        self.assertLessEqual(result.confidence, 1.0)


if __name__ == "__main__":
    unittest.main()

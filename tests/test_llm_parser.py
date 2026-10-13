from __future__ import annotations

import json
import sqlite3
import unittest

import httpx

from src.analyzer.llm_analyzer import LLMAnalyzer
from src.errors import CollectorConfigError, LLMResponseError
from src.models.schema import FeedbackItem
from src.storage.repository import FeedbackRepository
from src.utils.config import AppConfig


def make_item(content: str = "Critical bug: the export crashed.", platform: str = "xhs") -> FeedbackItem:
    return FeedbackItem(
        id=f"{platform}:test:1",
        platform=platform,
        content=content,
        author="tester",
        created_at="2026-10-13T12:00:00+00:00",
        metrics={"likes": 1},
        url="https://www.xiaohongshu.com/explore/6a993ffe0000000011038025",
    )


class TestLLMResponseParsing(unittest.TestCase):
    """Tolerant parsing of real-world LLM responses."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.analyzer = LLMAnalyzer(AppConfig(openai_api_key=""), FeedbackRepository(self.conn))

    def tearDown(self):
        self.conn.close()

    def test_strips_markdown_json_fence(self):
        raw = """```json
{"sentiment": "negative", "category": "bug", "summary": "Data export failed", "confidence": 0.95}
```"""
        parsed = self.analyzer.parse_analysis_payload(raw)
        self.assertEqual(parsed["sentiment"], "negative")
        self.assertEqual(parsed["category"], "bug")
        self.assertAlmostEqual(parsed["confidence"], 0.95)

    def test_tolerates_surrounding_prose(self):
        raw = """Here is the structured analysis requested:
{"sentiment": "positive", "category": "praise", "summary": "Great tool speed", "confidence": 0.88}
Hope this helps!"""
        parsed = self.analyzer.parse_analysis_payload(raw)
        self.assertEqual(parsed["sentiment"], "positive")
        self.assertEqual(parsed["category"], "praise")

    def test_rejects_invalid_sentiment(self):
        raw = '{"sentiment": "angry", "category": "bug", "summary": "x", "confidence": 0.5}'
        with self.assertRaises(LLMResponseError):
            self.analyzer.parse_analysis_payload(raw)

    def test_rejects_invalid_category(self):
        raw = '{"sentiment": "negative", "category": "complaint", "summary": "x", "confidence": 0.5}'
        with self.assertRaises(LLMResponseError):
            self.analyzer.parse_analysis_payload(raw)

    def test_rejects_empty_summary(self):
        raw = '{"sentiment": "neutral", "category": "other", "summary": "  ", "confidence": 0.5}'
        with self.assertRaises(LLMResponseError):
            self.analyzer.parse_analysis_payload(raw)

    def test_rejects_response_without_json_object(self):
        with self.assertRaises(LLMResponseError):
            self.analyzer.parse_analysis_payload("I could not analyze this.")

    def test_clamps_out_of_range_confidence(self):
        raw = '{"sentiment": "neutral", "category": "other", "summary": "ok", "confidence": 4.2}'
        self.assertEqual(self.analyzer.parse_analysis_payload(raw)["confidence"], 1.0)


class TestHeuristicIsOptIn(unittest.IsolatedAsyncioTestCase):
    """
    The rule engine must never run implicitly. An unconfigured key has to surface as
    a loud configuration error, because silently substituting keyword matching for an
    LLM is exactly the failure mode this project set out to eliminate.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.repo = FeedbackRepository(self.conn)

    def tearDown(self):
        self.conn.close()

    async def test_missing_key_raises_instead_of_falling_back(self):
        analyzer = LLMAnalyzer(AppConfig(openai_api_key=""), self.repo)
        async with httpx.AsyncClient() as client:
            with self.assertRaises(CollectorConfigError):
                await analyzer.analyze_item(client, make_item())

    async def test_heuristic_requires_explicit_opt_in_and_is_labelled(self):
        analyzer = LLMAnalyzer(AppConfig(openai_api_key=""), self.repo)
        item = make_item("Critical bug: the app crashed with a memory error.")
        async with httpx.AsyncClient() as client:
            result = await analyzer.analyze_item(client, item, allow_heuristic=True)

        self.assertEqual(result.sentiment, "negative")
        self.assertEqual(result.category, "bug")
        self.assertEqual(result.feedback_id, item.id)
        # The stamp is what keeps this distinguishable from real model output.
        self.assertEqual(result.model, "heuristic-rule-engine-v1")
        self.assertEqual(result.validate(), [])


if __name__ == "__main__":
    unittest.main()

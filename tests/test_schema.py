from __future__ import annotations

import unittest
from src.models.schema import AnalysisResult, FeedbackItem


class TestSchemaValidation(unittest.TestCase):
    """Test data contract validation rules."""

    def test_valid_feedback_item(self):
        item = FeedbackItem(
            id="discord:123:456",
            platform="discord",
            content="Testing feedback item content.",
            author="tester_user",
            created_at="2026-10-13T10:00:00Z",
            metrics={"likes": 5},
            url="https://discord.com/channels/123/456/789",
        )
        errors = item.validate()
        self.assertEqual(len(errors), 0, f"Expected no errors, got: {errors}")

    def test_invalid_missing_fields(self):
        item = FeedbackItem(
            id="",
            platform="unknown_plat",
            content="   ",
            author="",
            created_at="",
            url="not_a_valid_url",
        )
        errors = item.validate()
        self.assertGreater(len(errors), 3)
        self.assertTrue(any("id" in e for e in errors))
        self.assertTrue(any("platform" in e for e in errors))
        self.assertTrue(any("content" in e for e in errors))
        self.assertTrue(any("url" in e for e in errors))

    def test_analysis_result_validation(self):
        res = AnalysisResult(
            feedback_id="test_id_1",
            sentiment="negative",
            category="bug",
            summary="Application crashed on start",
            confidence=0.95,
            model="test-model",
        )
        self.assertEqual(len(res.validate()), 0)

        invalid_res = AnalysisResult(
            feedback_id="",
            sentiment="super_happy",  # invalid
            category="not_a_category",  # invalid
            summary="",
            confidence=1.5,  # out of bounds
            model="",
        )
        errors = invalid_res.validate()
        self.assertGreater(len(errors), 0)


if __name__ == "__main__":
    unittest.main()

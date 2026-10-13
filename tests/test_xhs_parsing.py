from __future__ import annotations

import unittest

from src.collectors.xhs_collector import (
    NOTE_ID_STRICT_RE,
    XHSCollector,
    parse_interaction_count,
)
from src.models.db import open_database
from src.storage.checkpoint import CheckpointManager
from src.storage.repository import FeedbackRepository
from src.utils.config import AppConfig


class TestInteractionCountParsing(unittest.TestCase):
    """Xiaohongshu abbreviates large counters with Chinese myriad units."""

    def test_plain_integer(self):
        self.assertEqual(parse_interaction_count("1528"), 1528)

    def test_thousands_separator(self):
        self.assertEqual(parse_interaction_count("3,204"), 3204)

    def test_ten_thousand_unit(self):
        self.assertEqual(parse_interaction_count("1.2万"), 12000)

    def test_hundred_million_unit(self):
        self.assertEqual(parse_interaction_count("2亿"), 200_000_000)

    def test_lowercase_w_suffix(self):
        self.assertEqual(parse_interaction_count("3.5w"), 35000)

    def test_noise_around_the_number(self):
        self.assertEqual(parse_interaction_count("点赞 88"), 88)

    def test_unparseable_is_zero(self):
        for raw in ("", "   ", "点赞", None):
            self.assertEqual(parse_interaction_count(raw), 0)


class TestNoteIdValidation(unittest.TestCase):
    """
    Promoted slots reuse the `note-item` markup but carry UUID-style ids. Accepting
    them would inject advertisement rows with no publish time into the dataset.
    """

    def test_canonical_note_id_is_accepted(self):
        self.assertTrue(NOTE_ID_STRICT_RE.match("6a993ffe0000000011038025"))

    def test_promoted_uuid_style_id_is_rejected(self):
        self.assertIsNone(
            NOTE_ID_STRICT_RE.match("4c3d806d-4209-4dfa-b13c-4e3e9d0f94ad#1791896989117")
        )

    def test_wrong_length_is_rejected(self):
        self.assertIsNone(NOTE_ID_STRICT_RE.match("6a993ffe"))
        self.assertIsNone(NOTE_ID_STRICT_RE.match("6a993ffe00000000110380250000"))


class TestCardToFeedbackItem(unittest.TestCase):
    """Normalisation of a raw search card into the unified data contract."""

    def setUp(self):
        self.conn = open_database("sqlite:///:memory:")
        config = AppConfig(database_url="sqlite:///:memory:")
        self.collector = XHSCollector(
            config, FeedbackRepository(self.conn), CheckpointManager(self.conn)
        )

    def tearDown(self):
        self.conn.close()

    def test_builds_a_contract_valid_item(self):
        item = self.collector._card_to_feedback_item(
            {
                "note_id": "6a993ffe0000000011038025",
                "title": "AI 工具怎么选？一张图讲清楚",
                "author": "AI小白进化笔记",
                "time_text": "08-19",
                "like_text": "2198",
                "href": "/search_result/6a993ffe0000000011038025?xsec_token=x",
                "keyword": "AI工具",
            }
        )
        self.assertEqual(item.id, "xhs:6a993ffe0000000011038025")
        self.assertEqual(item.platform, "xhs")
        self.assertEqual(item.author, "AI小白进化笔记")
        self.assertEqual(item.metrics["likes"], 2198)
        self.assertEqual(item.url, "https://www.xiaohongshu.com/explore/6a993ffe0000000011038025")
        self.assertEqual(item.validate(), [])

    def test_missing_author_falls_back_but_stays_valid(self):
        item = self.collector._card_to_feedback_item(
            {
                "note_id": "6a993ffe0000000011038025",
                "title": "标题",
                "author": "",
                "time_text": "刚刚",
                "like_text": "",
                "keyword": "AI工具",
            }
        )
        self.assertEqual(item.author, "未知作者")
        self.assertEqual(item.validate(), [])

    def test_missing_publish_time_is_reported_not_invented(self):
        item = self.collector._card_to_feedback_item(
            {
                "note_id": "6a993ffe0000000011038025",
                "title": "标题",
                "author": "某人",
                "time_text": "置顶",
                "like_text": "1",
                "keyword": "AI工具",
            }
        )
        self.assertEqual(item.created_at, "")
        # The contract must flag this rather than let a bogus timestamp through.
        self.assertIn(
            "Field 'created_at' is required (ISO 8601 timestamp).", item.validate()
        )

    def test_content_falls_back_to_card_text(self):
        item = self.collector._card_to_feedback_item(
            {
                "note_id": "6a993ffe0000000011038025",
                "title": "",
                "card_text": "整张卡片的可见文本",
                "author": "某人",
                "time_text": "刚刚",
                "like_text": "1",
                "keyword": "AI工具",
            }
        )
        self.assertEqual(item.content, "整张卡片的可见文本")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from src.utils.timeparse import parse_xhs_time


class TestXHSTimeParsing(unittest.TestCase):
    """
    Xiaohongshu renders publish times as relative or year-less strings. Getting this
    wrong would silently corrupt every time-series query downstream, so the
    resolution rules are pinned down explicitly here.
    """

    NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)

    def test_just_now(self):
        self.assertEqual(parse_xhs_time("刚刚", self.NOW), self.NOW.isoformat())

    def test_minutes_ago(self):
        self.assertTrue(parse_xhs_time("12分钟前", self.NOW).startswith("2026-10-13T11:48"))

    def test_hours_ago(self):
        self.assertTrue(parse_xhs_time("7小时前", self.NOW).startswith("2026-10-13T05:00"))

    def test_yesterday(self):
        self.assertTrue(parse_xhs_time("昨天", self.NOW).startswith("2026-10-12"))

    def test_days_ago(self):
        self.assertTrue(parse_xhs_time("5天前", self.NOW).startswith("2026-10-08"))

    def test_chinese_numeral_hours(self):
        self.assertTrue(parse_xhs_time("三小时前", self.NOW).startswith("2026-10-13T09:00"))

    def test_yearless_date_uses_current_year_when_not_in_future(self):
        # 08-19 is before 10-13, so it means this year.
        self.assertTrue(parse_xhs_time("08-19", self.NOW).startswith("2026-08-19"))

    def test_yearless_date_rolls_back_when_it_would_be_in_the_future(self):
        # 12-25 is after 10-13, so it must mean last year.
        self.assertTrue(parse_xhs_time("12-25", self.NOW).startswith("2025-12-25"))

    def test_explicit_full_date(self):
        self.assertTrue(parse_xhs_time("2024-08-19", self.NOW).startswith("2024-08-19"))

    def test_editorial_prefix_is_stripped(self):
        self.assertTrue(parse_xhs_time("编辑于 3天前", self.NOW).startswith("2026-10-10"))

    def test_unparseable_returns_empty_not_a_fabricated_time(self):
        for label in ("", "   ", "推荐", "置顶", "02-30", "not a date"):
            self.assertEqual(parse_xhs_time(label, self.NOW), "", f"{label!r} should not parse")


if __name__ == "__main__":
    unittest.main()

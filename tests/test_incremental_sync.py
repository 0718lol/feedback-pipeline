from __future__ import annotations

import contextlib
import unittest

from src.collectors.xhs_collector import XHSCollector
from src.models.db import open_database
from src.models.schema import CollectorRunStats
from src.storage.checkpoint import CheckpointManager
from src.storage.repository import FeedbackRepository
from src.utils.config import AppConfig

# Two disjoint pages of fake search results, keyed by canonical 24-char note ids.
PAGE_1 = [
    {"note_id": "6a993ffe0000000011038025", "title": "AI 工具怎么选", "author": "甲", "time_text": "08-19", "like_text": "2198"},
    {"note_id": "6a97f4d4000000000d02737b", "title": "用 AI 做了个小工具", "author": "乙", "time_text": "3天前", "like_text": "412"},
    {"note_id": "6a0eed1f00000000070139e3", "title": "每天认识一个 AI 产品", "author": "丙", "time_text": "7小时前", "like_text": "5206"},
]
PAGE_2_EXTRA = [
    {"note_id": "6acdc3dc0000000018015d8d", "title": "开源工具 REA", "author": "丁", "time_text": "1天前", "like_text": "8"},
]


class _FakeMouse:
    async def wheel(self, x: int, y: int) -> None:  # pragma: no cover - trivial stub
        return None


class _FakePage:
    def __init__(self) -> None:
        self.mouse = _FakeMouse()

    async def goto(self, *args, **kwargs):
        return None


class _StubXHSCollector(XHSCollector):
    """
    Drives the real fetch -> persist -> commit pipeline without a browser, so the
    watermark and seen-set behaviour is exercised end to end rather than asserted
    against a mock of itself.
    """

    def __init__(self, *args, cards, **kwargs):
        super().__init__(*args, **kwargs)
        self._cards = cards

    def has_saved_session(self) -> bool:
        return True

    @contextlib.asynccontextmanager
    async def _browser_context(self, headless: bool, use_saved_session: bool = True):
        yield None, _FakePage()

    async def _detect_page_state(self, page) -> str:
        return "ready"

    async def _extract_cards(self, page, keyword: str, limit: int):
        return list(self._cards[:limit])


class TestIncrementalSync(unittest.IsolatedAsyncioTestCase):
    """
    Acceptance criteria:
      * '第二次运行只采到新增内容（增量同步验证）'
      * '连续运行 3 次，数据库无重复记录（幂等验证）'
    """

    def setUp(self):
        self.conn = open_database("sqlite:///:memory:")
        self.repo = FeedbackRepository(self.conn)
        self.checkpoints = CheckpointManager(self.conn)
        self.config = AppConfig(
            database_url="sqlite:///:memory:",
            xhs_search_keywords=["AI工具"],
            xhs_max_scrolls=3,
            xhs_stop_after_seen=99,
            xhs_max_notes_per_run=50,
            # Zero jitter so the suite stays fast; the real default is 2-5s.
            min_delay_seconds=0.0,
            max_delay_seconds=0.0,
        )

    def tearDown(self):
        self.conn.close()

    def _collector(self, cards):
        return _StubXHSCollector(self.config, self.repo, self.checkpoints, cards=cards)

    async def _run(self, cards) -> CollectorRunStats:
        stats = await self._collector(cards).run()
        self.assertEqual(len(stats), 1)
        return stats[0]

    def _count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM feedbacks").fetchone()[0]

    async def test_first_run_stores_everything(self):
        stats = await self._run(PAGE_1)
        self.assertEqual(stats.status, "success")
        self.assertEqual(stats.items_collected, 3)
        self.assertEqual(stats.items_new, 3)
        self.assertEqual(self._count(), 3)

    async def test_second_run_yields_no_new_items(self):
        await self._run(PAGE_1)
        stats = await self._run(PAGE_1)
        self.assertEqual(stats.items_collected, 0)
        self.assertEqual(stats.items_new, 0)
        self.assertEqual(self._count(), 3, "an unchanged search page must not add rows")

    async def test_three_identical_runs_leave_no_duplicates(self):
        for _ in range(3):
            await self._run(PAGE_1)
        self.assertEqual(self._count(), 3)

    async def test_later_run_only_picks_up_the_new_note(self):
        await self._run(PAGE_1)
        stats = await self._run(PAGE_1 + PAGE_2_EXTRA)
        self.assertEqual(stats.items_collected, 1)
        self.assertEqual(stats.items_new, 1)
        self.assertEqual(self._count(), 4)

    async def test_watermark_is_not_advanced_before_persistence(self):
        """
        Durability guard. If the process dies after fetching but before the rows are
        stored, nothing may be marked as 'seen' -- otherwise those notes would be
        skipped forever and silently lost.
        """
        collector = self._collector(PAGE_1)
        items = await collector.fetch_target_items("AI工具")
        self.assertEqual(len(items), 3)

        # Simulate a crash right here: fetched, never persisted, never committed.
        self.assertEqual(self.checkpoints.get_seen_ids("xhs:AI工具"), set())

        # A restart must therefore re-fetch the same three notes.
        recovered = await self._collector(PAGE_1).fetch_target_items("AI工具")
        self.assertEqual(len(recovered), 3)

    async def test_collected_items_satisfy_the_data_contract(self):
        await self._run(PAGE_1)
        rows = self.conn.execute("SELECT * FROM feedbacks").fetchall()
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertTrue(row["url"].startswith("https://www.xiaohongshu.com/explore/"))
            self.assertTrue(row["created_at"], "publish time must be resolved from the card label")
            self.assertNotEqual(row["author"], "")

    async def test_like_counts_are_parsed_into_integers(self):
        await self._run(PAGE_1)
        row = self.conn.execute(
            "SELECT metrics FROM feedbacks WHERE id = ?",
            ("xhs:6a993ffe0000000011038025",),
        ).fetchone()
        self.assertIn('"likes": 2198', row["metrics"])


if __name__ == "__main__":
    unittest.main()

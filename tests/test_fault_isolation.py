from __future__ import annotations

import unittest
from src.models.schema import CollectorRunStats
from src.pipeline import FeedbackPipeline
from src.utils.config import AppConfig


class TestFaultIsolation(unittest.IsolatedAsyncioTestCase):
    """
    Acceptance Criteria Test:
    '故意填错一个平台的配置，另一平台照常运行（异常隔离验证）'
    """

    async def asyncSetUp(self):
        self.config = AppConfig(
            database_url="sqlite:///:memory:",
            discord_bot_token="invalid_token_123",  # Intentionally wrong
            xhs_search_keywords=["AI测试"],
        )

    async def test_pipeline_isolates_failing_collector(self):
        pipeline = FeedbackPipeline(self.config)

        # Mock discord collector to raise an unhandled exception (simulating network crash or bad token)
        async def failing_run(*args, **kwargs):
            raise ConnectionError("Simulated network outage for Discord")

        async def succeeding_run(*args, **kwargs):
            return [
                CollectorRunStats(
                    run_id="xhs_run_1",
                    platform="xhs",
                    target="AI测试",
                    status="success",
                    items_collected=3,
                    items_new=3,
                )
            ]

        pipeline.collectors["discord"].run = failing_run
        pipeline.collectors["xhs"].run = succeeding_run

        # Execute collectors
        stats = await pipeline.run_collectors()

        # Discord failed, but XHS succeeded!
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0].platform, "xhs")
        self.assertEqual(stats[0].status, "success")
        self.assertEqual(stats[0].items_collected, 3)

        pipeline.close()


if __name__ == "__main__":
    unittest.main()

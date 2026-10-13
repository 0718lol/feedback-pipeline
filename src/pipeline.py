from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

from src.analyzer.llm_analyzer import LLMAnalyzer
from src.collectors.base import BaseCollector
from src.collectors.discord_collector import DiscordCollector
from src.collectors.xhs_collector import XHSCollector
from src.models.db import open_database, resolve_db_path
from src.models.schema import CollectorRunStats
from src.storage.checkpoint import CheckpointManager
from src.storage.repository import FeedbackRepository
from src.utils.config import AppConfig
from src.utils.logger import setup_logger


class FeedbackPipeline:
    """
    Master Pipeline Orchestrator.
    Coordinates multi-platform collection, incremental synchronization,
    fault isolation, database persistence, and LLM analysis.
    """

    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or AppConfig.load()
        self.logger = setup_logger("feedback_pipeline.orchestrator")

        # Resolve the database from config (falling back to the DATABASE_URL env var).
        # Never rely on an implicit global default -- otherwise a test that passes
        # database_url="sqlite:///:memory:" would silently write into the developer's
        # real data/feedbacks.db instead.
        self.db_path = resolve_db_path(self.config.database_url)
        self.conn = open_database(self.config.database_url)
        self.repo = FeedbackRepository(self.conn)
        self.checkpoint_mgr = CheckpointManager(self.conn)

        # Collectors
        self.collectors: Dict[str, BaseCollector] = {
            "discord": DiscordCollector(self.config, self.repo, self.checkpoint_mgr),
            "xhs": XHSCollector(self.config, self.repo, self.checkpoint_mgr),
        }

        # Analyzer
        self.analyzer = LLMAnalyzer(self.config, self.repo)

    async def run_collectors(self, platforms: Optional[List[str]] = None) -> List[CollectorRunStats]:
        """
        Run collectors with Fault Isolation:
        If one platform fails or throws an exception, the other platform continues.
        """
        all_stats: List[CollectorRunStats] = []
        selected_platforms = platforms or list(self.collectors.keys())

        for platform_name in selected_platforms:
            collector = self.collectors.get(platform_name)
            if not collector:
                self.logger.warning(f"Platform collector '{platform_name}' not found. Skipping.")
                continue

            self.logger.info(f"=== Starting Collector: [{platform_name.upper()}] ===")
            try:
                platform_stats = await collector.run()
                all_stats.extend(platform_stats)
            except Exception as e:
                # Fault isolation guarantee
                self.logger.error(
                    f"[ISOLATION] Unexpected fatal error in collector '{platform_name}': {e}. "
                    "Isolating error so other platforms proceed safely."
                )

        return all_stats

    async def run_analysis(
        self, batch_size: int = 100, allow_heuristic: bool = False
    ) -> Dict[str, int]:
        """Analyze pending unanalyzed feedback items in the database."""
        self.logger.info("=== Starting LLM Analysis Stage ===")
        return await self.analyzer.process_pending_feedbacks(
            batch_size=batch_size, allow_heuristic=allow_heuristic
        )

    async def execute_full_pipeline(
        self,
        platforms: Optional[List[str]] = None,
        batch_size: int = 100,
        allow_heuristic: bool = False,
    ) -> Dict[str, any]:
        """
        Execute end-to-end pipeline:
        1. Multi-platform incremental collection
        2. LLM structured analysis
        3. Quality summary & telemetry report
        """
        self.logger.info("**************************************************")
        self.logger.info("   Starting Full Feedback Pipeline Execution      ")
        self.logger.info("**************************************************")

        collector_stats = await self.run_collectors(platforms)
        analysis_stats = await self.run_analysis(
            batch_size=batch_size, allow_heuristic=allow_heuristic
        )
        summary = self.repo.get_summary_statistics()

        self.logger.info("=== Pipeline Execution Complete ===")
        self.logger.info(f"Database Total Feedbacks: {summary['total_feedbacks']}")
        self.logger.info(f"Platform Breakdown: {summary['platform_breakdown']}")
        self.logger.info(f"Total Analyzed: {summary['total_analyzed']}")
        self.logger.info(f"Sentiment Distribution: {summary['sentiment_distribution']}")

        failed_runs = [s for s in collector_stats if s.status == "failed"]
        if failed_runs:
            self.logger.error(
                f"QUALITY ALERT: {len(failed_runs)} collector target(s) failed this run: "
                + ", ".join(f"{s.platform}:{s.target}" for s in failed_runs)
            )

        return {
            "collector_stats": [s.to_dict() for s in collector_stats],
            "analysis_stats": analysis_stats,
            "database_summary": summary,
        }

    def close(self) -> None:
        if self.conn:
            self.conn.close()

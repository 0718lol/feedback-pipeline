from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

from src.analyzer.llm_analyzer import LLMAnalyzer
from src.collectors.base import BaseCollector
from src.collectors.discord_collector import DiscordCollector
from src.collectors.xhs_collector import XHSCollector
from src.models.db import get_db_connection, init_db
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

        # Initialize SQLite schema
        init_db()
        self.conn = get_db_connection()
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

    def run_analysis(self, batch_size: int = 100) -> int:
        """Analyze pending unanalyzed feedback items in database."""
        self.logger.info("=== Starting LLM Analysis Stage ===")
        return self.analyzer.process_pending_feedbacks(batch_size=batch_size)

    async def execute_full_pipeline(self, platforms: Optional[List[str]] = None) -> Dict[str, any]:
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
        analyzed_count = self.run_analysis()
        summary = self.repo.get_summary_statistics()

        self.logger.info("=== Pipeline Execution Complete ===")
        self.logger.info(f"Database Total Feedbacks: {summary['total_feedbacks']}")
        self.logger.info(f"Platform Breakdown: {summary['platform_breakdown']}")
        self.logger.info(f"Total Analyzed: {summary['total_analyzed']}")
        self.logger.info(f"Sentiment Distribution: {summary['sentiment_dist' if 'sentiment_dist' in summary else 'sentiment_distribution']}")

        return {
            "collector_stats": [s.to_dict() for s in collector_stats],
            "analyzed_count": analyzed_count,
            "database_summary": summary,
        }

    def close(self) -> None:
        if self.conn:
            self.conn.close()

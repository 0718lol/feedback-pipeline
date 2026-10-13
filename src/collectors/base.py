from __future__ import annotations

import abc
import time
import uuid
from typing import List, Optional

from src.models.schema import CollectorRunStats, FeedbackItem, current_iso_time
from src.storage.checkpoint import CheckpointManager
from src.storage.repository import FeedbackRepository
from src.utils.config import AppConfig
from src.utils.logger import setup_logger


class BaseCollector(abc.ABC):
    """
    Abstract Base Class for Platform Collectors.
    Encapsulates:
    1. Checkpoint & Watermark lifecycle
    2. Idempotent storage execution
    3. Telemetry and Run metrics recording
    4. Quality monitoring & alerting triggers
    5. Fault isolation (exceptions are contained and recorded, never killing the master pipeline)
    """

    def __init__(
        self,
        config: AppConfig,
        repository: FeedbackRepository,
        checkpoint_manager: CheckpointManager,
    ):
        self.config = config
        self.repo = repository
        self.checkpoint_mgr = checkpoint_manager
        self.logger = setup_logger(f"collector.{self.platform_name}")

    @property
    @abc.abstractmethod
    def platform_name(self) -> str:
        """Name of the platform, e.g. 'discord' or 'xhs'."""
        pass

    @abc.abstractmethod
    async def fetch_target_items(self, target: str) -> List[FeedbackItem]:
        """
        Subclass implementation to extract items from a given target
        (e.g., channel_id or search keyword).
        Must respect checkpoints and return standardized FeedbackItems.
        """
        pass

    @abc.abstractmethod
    def get_targets(self) -> List[str]:
        """Return list of targets to process (e.g. channels or search keywords)."""
        pass

    async def run(self) -> List[CollectorRunStats]:
        """
        Execute collection across all configured targets with fault isolation.
        """
        results: List[CollectorRunStats] = []
        targets = self.get_targets()

        if not targets:
            self.logger.warning(f"No targets configured for platform '{self.platform_name}'.")
            return results

        for target in targets:
            run_id = f"{self.platform_name}_{target}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            stats = CollectorRunStats(
                run_id=run_id,
                platform=self.platform_name,
                target=target,
                status="running",
                start_time=current_iso_time(),
            )

            try:
                self.logger.info(f"[{self.platform_name}] Starting collection for target: '{target}'")
                items = await self.fetch_target_items(target)
                stats.items_collected = len(items)

                # Quality Alert: 0 items collected
                if stats.items_collected == 0:
                    self.logger.warning(
                        f"[{self.platform_name}] QUALITY ALERT: 0 items collected for target '{target}'. "
                        "Check search criteria, channel activity, or authentication state."
                    )

                # Field completeness check
                incomplete_count = 0
                for item in items:
                    validation_errors = item.validate()
                    if validation_errors:
                        incomplete_count += 1
                        self.logger.warning(
                            f"[{self.platform_name}] Item {item.id} contract violation: {validation_errors}"
                        )

                if items and (incomplete_count / len(items) > 0.2):
                    self.logger.error(
                        f"[{self.platform_name}] QUALITY ALERT: High contract error rate ({incomplete_count}/{len(items)}). "
                        "Page structure or API format may have changed!"
                    )

                # Idempotent batch insertion
                new_count, dup_count = self.repo.save_feedbacks_batch(items)
                stats.items_new = new_count
                stats.items_failed = incomplete_count
                stats.status = "success" if incomplete_count == 0 else "warning"

                self.logger.info(
                    f"[{self.platform_name}] Completed '{target}': {stats.items_collected} collected, "
                    f"{stats.items_new} new, {dup_count} duplicates ignored."
                )

            except Exception as e:
                self.logger.exception(
                    f"[{self.platform_name}] Failed collection for target '{target}': {e}"
                )
                stats.status = "failed"
                stats.error_message = str(e)
            finally:
                stats.end_time = current_iso_time()
                self.repo.record_run_stats(stats)
                results.append(stats)

        return results

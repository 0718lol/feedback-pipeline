from __future__ import annotations

import abc
import time
import uuid
from typing import List, Optional

from src.errors import (
    CollectorConfigError,
    LoginRequiredError,
    PlatformBlockedError,
)
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

    def commit_checkpoint(self, target: str, items: List[FeedbackItem]) -> None:
        """
        Advance this target's watermark/seen-set once `items` are safely persisted.

        The ordering matters: BaseCollector.run() calls this *after* the database
        write succeeds, so a crash between fetching and storing can never mark data
        as "already collected" that was never actually stored. Subclasses that track
        a cursor or a seen-set override this.
        """
        return None

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

                # Quality monitoring. "0 items" is only alarming when this target has
                # never yielded anything before; a target with a populated seen-set
                # returning 0 is simply incremental convergence, which is the system
                # working as designed.
                previously_seen = len(self.checkpoint_mgr.get_seen_ids(
                    f"{self.platform_name}:{target}"
                ))
                if stats.items_collected == 0:
                    if previously_seen:
                        self.logger.info(
                            f"[{self.platform_name}] No new items for target '{target}' "
                            f"(incremental sync; {previously_seen} items already stored)."
                        )
                    else:
                        stats.status = "warning"
                        self.logger.warning(
                            f"[{self.platform_name}] QUALITY ALERT: 0 items collected for target "
                            f"'{target}' and nothing was ever stored for it. Check search "
                            "criteria, channel activity, selector drift or authentication state."
                        )

                # Field completeness check against the unified data contract.
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
                        f"[{self.platform_name}] QUALITY ALERT: High contract error rate "
                        f"({incomplete_count}/{len(items)}). Page structure or API format "
                        "may have changed!"
                    )

                # Idempotent batch insertion (primary key + INSERT OR IGNORE).
                new_count, dup_count = self.repo.save_feedbacks_batch(items)
                stats.items_new = new_count
                stats.items_failed = incomplete_count
                if incomplete_count == 0 and stats.status == "running":
                    stats.status = "success"
                elif incomplete_count:
                    stats.status = "warning"

                # Only now that the rows are durable do we advance the watermark.
                self.commit_checkpoint(target, items)

                self.logger.info(
                    f"[{self.platform_name}] Completed '{target}': {stats.items_collected} collected, "
                    f"{stats.items_new} new, {dup_count} duplicates ignored."
                )

            except (CollectorConfigError, LoginRequiredError, PlatformBlockedError) as e:
                # Expected, actionable operational conditions: log the message without a
                # misleading traceback, and record the real reason in the run telemetry.
                stats.status = "failed"
                stats.error_message = str(e)
                self.logger.error(
                    f"[{self.platform_name}] Cannot collect target '{target}': {e}"
                )

            except Exception as e:
                stats.status = "failed"
                stats.error_message = f"{type(e).__name__}: {e}"
                self.logger.exception(
                    f"[{self.platform_name}] Unexpected failure for target '{target}': {e}"
                )
                stats.status = "failed"
                stats.error_message = str(e)
            finally:
                stats.end_time = current_iso_time()
                self.repo.record_run_stats(stats)
                results.append(stats)

        return results

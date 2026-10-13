from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from src.models.schema import AnalysisResult, CollectorRunStats, FeedbackItem


class FeedbackRepository:
    """
    Data Access Object (DAO) providing idempotent persistence,
    querying, and traceability linkages for user feedback items.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def save_feedback(self, item: FeedbackItem) -> bool:
        """
        Idempotently insert a single feedback item.
        Returns:
            True if newly inserted, False if record already exists (duplicate ignored).
        """
        metrics_json = json.dumps(item.metrics, ensure_ascii=False)
        raw_data_json = json.dumps(item.raw_data, ensure_ascii=False)

        cursor = self.conn.cursor()
        with self.conn:
            cursor.execute(
                """
                INSERT OR IGNORE INTO feedbacks (
                    id, platform, content, author, created_at,
                    metrics, url, source_target, raw_data, collected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    item.id,
                    item.platform,
                    item.content,
                    item.author,
                    item.created_at,
                    metrics_json,
                    item.url,
                    item.source_target,
                    raw_data_json,
                    item.collected_at,
                ),
            )
            # If changes == 1, row was newly inserted; if 0, duplicate was ignored.
            return cursor.rowcount > 0

    def save_feedbacks_batch(self, items: List[FeedbackItem]) -> Tuple[int, int]:
        """
        Idempotently save a list of feedback items.
        Returns:
            (inserted_count, duplicate_count)
        """
        inserted = 0
        duplicates = 0
        for item in items:
            if self.save_feedback(item):
                inserted += 1
            else:
                duplicates += 1
        return inserted, duplicates

    def get_feedback_by_id(self, feedback_id: str) -> Optional[FeedbackItem]:
        """Retrieve a FeedbackItem by unique ID."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM feedbacks WHERE id = ?", (feedback_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return FeedbackItem.from_dict(dict(row))

    def get_unanalyzed_feedbacks(self, limit: int = 100) -> List[FeedbackItem]:
        """
        Retrieve feedback items that do not yet have an entry in analysis_results.
        Used for downstream LLM processing.
        """
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT f.*
            FROM feedbacks f
            LEFT JOIN analysis_results a ON f.id = a.feedback_id
            WHERE a.feedback_id IS NULL
            ORDER BY f.created_at DESC
            LIMIT ?;
            """,
            (limit,),
        )
        return [FeedbackItem.from_dict(dict(row)) for row in cursor.fetchall()]

    def save_analysis(self, result: AnalysisResult) -> bool:
        """
        Idempotently insert or update an LLM analysis result.
        """
        cursor = self.conn.cursor()
        with self.conn:
            cursor.execute(
                """
                INSERT INTO analysis_results (
                    feedback_id, sentiment, category, summary, confidence, model, analyzed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(feedback_id) DO UPDATE SET
                    sentiment = excluded.sentiment,
                    category = excluded.category,
                    summary = excluded.summary,
                    confidence = excluded.confidence,
                    model = excluded.model,
                    analyzed_at = excluded.analyzed_at;
                """,
                (
                    result.feedback_id,
                    result.sentiment,
                    result.category,
                    result.summary,
                    result.confidence,
                    result.model,
                    result.analyzed_at,
                ),
            )
            return cursor.rowcount > 0

    def get_traceable_feedback(self, feedback_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch joined feedback item, analysis result, and direct verifiable origin URL.
        Guarantees Section 5 Requirement: '可追溯性'
        """
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT 
                f.id, f.platform, f.content, f.author, f.created_at, f.url, f.metrics,
                a.sentiment, a.category, a.summary, a.confidence, a.model, a.analyzed_at
            FROM feedbacks f
            LEFT JOIN analysis_results a ON f.id = a.feedback_id
            WHERE f.id = ?;
            """,
            (feedback_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        if isinstance(res.get("metrics"), str):
            try:
                res["metrics"] = json.loads(res["metrics"])
            except Exception:
                pass
        return res

    def record_run_stats(self, stats: CollectorRunStats) -> None:
        """Log collector run telemetry."""
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO collection_runs (
                    run_id, platform, target, status, items_collected,
                    items_new, items_failed, start_time, end_time, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status = excluded.status,
                    items_collected = excluded.items_collected,
                    items_new = excluded.items_new,
                    items_failed = excluded.items_failed,
                    end_time = excluded.end_time,
                    error_message = excluded.error_message;
                """,
                (
                    stats.run_id,
                    stats.platform,
                    stats.target,
                    stats.status,
                    stats.items_collected,
                    stats.items_new,
                    stats.items_failed,
                    stats.start_time,
                    stats.end_time,
                    stats.error_message,
                ),
            )

    def get_summary_statistics(self) -> Dict[str, Any]:
        """Return system-wide statistics for monitoring and health check."""
        cursor = self.conn.cursor()

        cursor.execute("SELECT COUNT(*) AS total FROM feedbacks;")
        total_feedbacks = cursor.fetchone()["total"]

        cursor.execute("SELECT platform, COUNT(*) AS cnt FROM feedbacks GROUP BY platform;")
        platform_counts = {row["platform"]: row["cnt"] for row in cursor.fetchall()}

        cursor.execute("SELECT COUNT(*) AS total FROM analysis_results;")
        total_analyzed = cursor.fetchone()["total"]

        cursor.execute(
            "SELECT sentiment, COUNT(*) AS cnt FROM analysis_results GROUP BY sentiment;"
        )
        sentiment_dist = {row["sentiment"]: row["cnt"] for row in cursor.fetchall()}

        cursor.execute(
            "SELECT category, COUNT(*) AS cnt FROM analysis_results GROUP BY category;"
        )
        category_dist = {row["category"]: row["cnt"] for row in cursor.fetchall()}

        cursor.execute("SELECT COUNT(*) AS total FROM collection_runs;")
        total_runs = cursor.fetchone()["total"]

        return {
            "total_feedbacks": total_feedbacks,
            "platform_breakdown": platform_counts,
            "total_analyzed": total_analyzed,
            "unanalyzed_feedbacks": total_feedbacks - total_analyzed,
            "sentiment_distribution": sentiment_dist,
            "category_distribution": category_dist,
            "total_collection_runs": total_runs,
        }

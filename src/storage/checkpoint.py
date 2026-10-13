from __future__ import annotations

import sqlite3
from typing import Optional, Tuple
from src.models.schema import current_iso_time


class CheckpointManager:
    """
    Manages incremental collection watermarks and breakpoint resume state.
    Each collection target (e.g., 'discord:channel_id' or 'xhs:keyword') has a unique key.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get_checkpoint(self, checkpoint_key: str) -> Tuple[Optional[str], Optional[str], int]:
        """
        Get the latest watermark for a target.
        Returns: (last_item_id, last_timestamp, items_count)
        """
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT last_item_id, last_timestamp, items_count
            FROM checkpoints
            WHERE checkpoint_key = ?
            """,
            (checkpoint_key,),
        )
        row = cursor.fetchone()
        if row:
            return row["last_item_id"], row["last_timestamp"], row["items_count"]
        return None, None, 0

    def update_checkpoint(
        self,
        checkpoint_key: str,
        last_item_id: Optional[str] = None,
        last_timestamp: Optional[str] = None,
        increment_count: int = 0,
    ) -> None:
        """
        Update or initialize the watermark for a target.
        """
        now = current_iso_time()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO checkpoints (checkpoint_key, last_item_id, last_timestamp, items_count, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(checkpoint_key) DO UPDATE SET
                    last_item_id = COALESCE(excluded.last_item_id, checkpoints.last_item_id),
                    last_timestamp = COALESCE(excluded.last_timestamp, checkpoints.last_timestamp),
                    items_count = checkpoints.items_count + excluded.items_count,
                    updated_at = excluded.updated_at;
                """,
                (checkpoint_key, last_item_id, last_timestamp, increment_count, now),
            )

    def reset_checkpoint(self, checkpoint_key: str) -> None:
        """Reset watermark to allow full re-collection if needed."""
        with self.conn:
            self.conn.execute("DELETE FROM checkpoints WHERE checkpoint_key = ?", (checkpoint_key,))

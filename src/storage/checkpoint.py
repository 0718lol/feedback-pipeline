from __future__ import annotations

import json
import sqlite3
from typing import Iterable, List, Optional, Set, Tuple

from src.models.schema import current_iso_time


class CheckpointManager:
    """
    Manages incremental collection watermarks and breakpoint resume state.
    Each collection target (e.g., 'discord:channel_id' or 'xhs:keyword') has a unique key.

    Two complementary mechanisms are supported:

    * A monotonic **watermark** (`last_item_id` / `last_timestamp`), used where the
      upstream API exposes a real cursor (e.g. Discord's `after=<message_id>`).
    * A **seen-id set**, used for relevance-ordered feeds (e.g. Xiaohongshu search
      results) where "the newest id" is meaningless because result order is not
      chronological. Here the only sound incremental rule is "skip ids we already
      persisted", which is exactly what the set provides.
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

    def get_seen_ids(self, checkpoint_key: str) -> Set[str]:
        """
        Return the set of item ids already persisted for this target.
        Empty set when the target has never run or the column is absent.
        """
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                "SELECT seen_ids FROM checkpoints WHERE checkpoint_key = ?", (checkpoint_key,)
            )
        except sqlite3.OperationalError:
            # Pre-migration database; the schema upgrade adds the column on next init.
            return set()
        row = cursor.fetchone()
        if not row:
            return set()
        raw = row["seen_ids"] if isinstance(row, sqlite3.Row) else row[0]
        if not raw:
            return set()
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return set()
        return set(str(x) for x in parsed) if isinstance(parsed, list) else set()

    def add_seen_ids(self, checkpoint_key: str, item_ids: Iterable[str]) -> int:
        """
        Merge new ids into the target's seen-set and return the resulting set size.
        Idempotent: re-adding an existing id is a no-op.
        """
        new_ids = {str(i) for i in item_ids if i}
        if not new_ids:
            return len(self.get_seen_ids(checkpoint_key))

        merged = self.get_seen_ids(checkpoint_key) | new_ids
        now = current_iso_time()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO checkpoints (checkpoint_key, seen_ids, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(checkpoint_key) DO UPDATE SET
                    seen_ids = excluded.seen_ids,
                    updated_at = excluded.updated_at;
                """,
                (checkpoint_key, json.dumps(sorted(merged)), now),
            )
        return len(merged)

    def reset_checkpoint(self, checkpoint_key: str) -> None:
        """Reset watermark to allow full re-collection if needed."""
        with self.conn:
            self.conn.execute("DELETE FROM checkpoints WHERE checkpoint_key = ?", (checkpoint_key,))

"""
Discord feedback collector using the official Discord REST API (v10).

Compliance: this uses a bot token you own, against channels the bot has been
explicitly invited to. There is no user-account automation and no scraping of
private data.

Engineering notes:
  * Requests go through httpx (async), matching the rest of the project's HTTP stack.
  * Incremental sync uses Discord's native `after=<message_id>` cursor, which is a
    genuine monotonic cursor -- unlike a relevance-ordered web feed, this one can be
    trusted as a watermark.
  * When no token is configured the collector fails loudly. It never substitutes
    fabricated messages.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import httpx

from src.collectors.base import BaseCollector
from src.errors import CollectorConfigError, CollectorError
from src.models.schema import FeedbackItem, current_iso_time
from src.utils.retry import async_random_sleep, retry_with_backoff

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_MESSAGE_LIMIT = 100


class DiscordCollector(BaseCollector):
    """Collects keyword-matching messages from configured Discord channels."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # channel_id -> (newest_message_id, timestamp), staged during fetch and
        # flushed by commit_checkpoint() once the rows are actually persisted.
        self._pending_watermarks: Dict[str, Tuple[str, str]] = {}

    @property
    def platform_name(self) -> str:
        return "discord"

    def get_targets(self) -> List[str]:
        return list(self.config.discord_target_channel_ids)

    def _format_discord_url(self, guild_id: str, channel_id: str, message_id: str) -> str:
        return f"https://discord.com/chat/{guild_id or '@me'}/{channel_id}/{message_id}"

    # ------------------------------------------------------------------ fetching

    @retry_with_backoff(max_retries=3, initial_delay=2.0)
    async def _fetch_messages_api(
        self,
        client: httpx.AsyncClient,
        channel_id: str,
        after_id: Optional[str] = None,
        limit: int = DISCORD_MESSAGE_LIMIT,
    ) -> List[Dict[str, Any]]:
        """Fetch one page of messages, honouring Discord's rate-limit headers."""
        url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
        params: Dict[str, Any] = {"limit": limit}
        if after_id:
            params["after"] = after_id

        response = await client.get(url, params=params)

        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "5"))
            self.logger.warning(
                f"[discord] Rate limited (429) on channel {channel_id}; "
                f"Discord asked for {retry_after}s."
            )
            raise CollectorError(f"Rate limited by Discord API, retry after {retry_after}s")

        if response.status_code in (401, 403):
            raise CollectorConfigError(
                f"Discord rejected the bot token (HTTP {response.status_code}) for channel "
                f"{channel_id}. Check DISCORD_BOT_TOKEN and that the bot is in that channel."
            )

        if response.status_code == 404:
            raise CollectorConfigError(
                f"Discord channel {channel_id} not found (HTTP 404). Check "
                "DISCORD_TARGET_CHANNEL_IDS."
            )

        response.raise_for_status()
        return response.json()

    # -------------------------------------------------------------------- main

    async def fetch_target_items(self, target: str) -> List[FeedbackItem]:
        channel_id = target
        checkpoint_key = f"discord:{channel_id}"

        token = (self.config.discord_bot_token or "").strip()
        if not token:
            raise CollectorConfigError(
                "DISCORD_BOT_TOKEN is not configured. Set it in .env to collect Discord "
                "feedback, or restrict the run to another platform with "
                "`--platform xhs`."
            )

        last_item_id, last_timestamp, _ = self.checkpoint_mgr.get_checkpoint(checkpoint_key)
        self.logger.info(
            f"[discord] Watermark for channel {channel_id}: "
            f"last_item_id={last_item_id}, last_timestamp={last_timestamp}"
        )

        headers = {
            "Authorization": f"Bot {token}",
            "User-Agent": "FeedbackCollectorBot/1.0 (Python/DataEngineeringIntern)",
        }

        async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:
            raw_messages = await self._fetch_messages_api(
                client, channel_id=channel_id, after_id=last_item_id
            )

        await async_random_sleep(self.config.min_delay_seconds, self.config.max_delay_seconds)

        guild_id = self.config.discord_target_guild_ids[0] if self.config.discord_target_guild_ids else ""
        keywords = [k.lower() for k in self.config.discord_keywords]

        items: List[FeedbackItem] = []
        newest_item_id = last_item_id
        newest_timestamp = last_timestamp

        for msg in raw_messages:
            msg_id = str(msg.get("id"))
            content = msg.get("content", "") or ""

            if keywords:
                content_lower = content.lower()
                if not any(kw in content_lower for kw in keywords):
                    continue

            author_data = msg.get("author", {}) or {}
            author_name = author_data.get("username", "unknown_user")

            created_at = msg.get("timestamp") or current_iso_time()
            reactions = msg.get("reactions", []) or []
            total_reactions = sum(r.get("count", 0) for r in reactions)

            items.append(
                FeedbackItem(
                    id=f"discord:{channel_id}:{msg_id}",
                    platform="discord",
                    content=content,
                    author=author_name,
                    created_at=created_at,
                    metrics={
                        "reactions": total_reactions,
                        "reaction_details": reactions,
                    },
                    url=self._format_discord_url(guild_id, channel_id, msg_id),
                    source_target=channel_id,
                    raw_data=msg,
                    collected_at=current_iso_time(),
                )
            )

            # Discord snowflake ids are monotonic, so the max is a valid watermark.
            if newest_item_id is None or int(msg_id) > int(newest_item_id):
                newest_item_id = msg_id
                newest_timestamp = created_at

        if newest_item_id != last_item_id:
            # Deferred until the rows are durable; see commit_checkpoint below.
            self._pending_watermarks[channel_id] = (newest_item_id, newest_timestamp or "")
            self.logger.info(
                f"[discord] Fetched up to message {newest_item_id} "
                f"({len(items)} matching items; watermark will advance after commit)."
            )

        return items

    def commit_checkpoint(self, target: str, items: List[FeedbackItem]) -> None:
        """
        Advance the `after=` cursor and seen-set once the matching rows are stored.

        The cursor advances based on the newest *fetched* message rather than the
        newest *matching* one: a channel where nothing matches the keyword filter
        would otherwise be re-scanned in full on every single run.
        """
        channel_id = target
        checkpoint_key = f"discord:{channel_id}"

        watermark = self._pending_watermarks.pop(channel_id, None)
        if watermark:
            newest_item_id, newest_timestamp = watermark
            self.checkpoint_mgr.update_checkpoint(
                checkpoint_key=checkpoint_key,
                last_item_id=newest_item_id,
                last_timestamp=newest_timestamp,
                increment_count=len(items),
            )
            self.logger.info(
                f"[discord] Watermark committed at message {newest_item_id}."
            )

        if items:
            self.checkpoint_mgr.add_seen_ids(checkpoint_key, [i.id for i in items])

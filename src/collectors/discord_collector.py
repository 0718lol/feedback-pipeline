from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.collectors.base import BaseCollector
from src.models.schema import FeedbackItem, current_iso_time
from src.utils.retry import async_random_sleep, retry_with_backoff


class DiscordCollector(BaseCollector):
    """
    Discord Feedback Collector using Official Discord API.
    Compliant, stable, and zero anti-scrape risk.
    Features:
    - Incremental sync using Discord API's 'after' cursor
    - Local watermark checkpointing
    - Message keyword filtering
    - Complete traceability to discord.com/channels/.../{message_id}
    - Offline fallback fixture mode if token not provided
    """

    @property
    def platform_name(self) -> str:
        return "discord"

    def get_targets(self) -> List[str]:
        # Targets are channel IDs
        if self.config.discord_target_channel_ids:
            return self.config.discord_target_channel_ids
        return ["mock_channel_feedback"]

    def _format_discord_url(self, guild_id: str, channel_id: str, message_id: str) -> str:
        return f"https://discord.com/channels/{guild_id or '@me'}/{channel_id}/{message_id}"

    @retry_with_backoff(max_retries=3, initial_delay=2.0)
    async def _fetch_messages_api(
        self, channel_id: str, after_id: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Fetch messages from Discord REST API v10."""
        token = self.config.discord_bot_token
        if not token:
            return []

        url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit={limit}"
        if after_id:
            url += f"&after={after_id}"

        headers = {
            "Authorization": f"Bot {token}",
            "User-Agent": "FeedbackCollectorBot/1.0 (Python/DataEngineeringIntern)",
        }

        req = urllib.request.Request(url, headers=headers, method="GET")
        # Run blocking network call in thread executor to keep asyncio responsive
        import asyncio
        loop = asyncio.get_running_loop()

        def _do_req():
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    if resp.status == 200:
                        return json.loads(resp.read().decode("utf-8"))
                    return []
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry_after = float(e.headers.get("Retry-After", "5"))
                    self.logger.warning(f"[discord] Rate limited by Discord API. Waiting {retry_after}s.")
                    raise RuntimeError(f"Rate limited: 429, retry after {retry_after}")
                elif e.code in (401, 403):
                    self.logger.error(f"[discord] Auth error {e.code}: Check DISCORD_BOT_TOKEN permissions.")
                    return []
                raise

        return await loop.run_in_executor(None, _do_req)

    def _generate_mock_messages(self, channel_id: str, after_id: Optional[str]) -> List[Dict[str, Any]]:
        """
        Generate deterministic mock data for testing and offline execution
        when real Bot token is not configured.
        """
        raw_samples = [
            {
                "id": "120000000000000001",
                "content": "Found a critical bug in the data export: CSV format drops columns with null values!",
                "author": {"username": "alice_data", "id": "1001"},
                "timestamp": "2026-10-10T10:00:00.000000+00:00",
                "reactions": [{"count": 5, "emoji": {"name": "thumbsup"}}],
            },
            {
                "id": "120000000000000002",
                "content": "Feature request: Can we please get dark mode in the reporting dashboard? It hurts my eyes.",
                "author": {"username": "bob_analyst", "id": "1002"},
                "timestamp": "2026-10-11T14:30:00.000000+00:00",
                "reactions": [{"count": 12, "emoji": {"name": "heart"}}],
            },
            {
                "id": "120000000000000003",
                "content": "The application crashed with a memory error when running on large batch sizes.",
                "author": {"username": "charlie_ml", "id": "1003"},
                "timestamp": "2026-10-12T09:15:00.000000+00:00",
                "reactions": [{"count": 3, "emoji": {"name": "eyes"}}],
            },
            {
                "id": "120000000000000004",
                "content": "Just wanted to say the new async pipeline is blazing fast, great suggestion implementation!",
                "author": {"username": "diana_swe", "id": "1004"},
                "timestamp": "2026-10-13T08:00:00.000000+00:00",
                "reactions": [{"count": 8, "emoji": {"name": "rocket"}}],
            },
            {
                "id": "120000000000000005",
                "content": "General chat message without any keywords, just saying good morning everyone.",
                "author": {"username": "evan", "id": "1005"},
                "timestamp": "2026-10-13T09:00:00.000000+00:00",
                "reactions": [],
            },
        ]

        if after_id:
            # Incremental: only return items with ID strictly greater than after_id
            filtered = [msg for msg in raw_samples if int(msg["id"]) > int(after_id)]
            return filtered
        return raw_samples

    async def fetch_target_items(self, target: str) -> List[FeedbackItem]:
        channel_id = target
        checkpoint_key = f"discord:{channel_id}"

        # 1. Retrieve checkpoint watermark for incremental sync
        last_item_id, last_timestamp, _ = self.checkpoint_mgr.get_checkpoint(checkpoint_key)
        self.logger.info(
            f"[discord] Watermark for channel {channel_id}: last_item_id={last_item_id}, "
            f"last_timestamp={last_timestamp}"
        )

        # 2. Fetch raw messages (from API if token available, else mock test mode)
        if self.config.discord_bot_token:
            raw_messages = await self._fetch_messages_api(channel_id=channel_id, after_id=last_item_id)
        else:
            self.logger.info(
                f"[discord] DISCORD_BOT_TOKEN not configured. Running in simulated test mode for channel {channel_id}."
            )
            raw_messages = self._generate_mock_messages(channel_id=channel_id, after_id=last_item_id)

        # Jitter delay between calls (Rate limit compliance)
        await async_random_sleep(self.config.min_delay_seconds, self.config.max_delay_seconds)

        guild_id = self.config.discord_target_guild_ids[0] if self.config.discord_target_guild_ids else ""
        keywords = [k.lower() for k in self.config.discord_keywords]

        items: List[FeedbackItem] = []
        newest_item_id = last_item_id
        newest_timestamp = last_timestamp

        # 3. Process, filter by keywords, and normalize to standard schema
        for msg in raw_messages:
            msg_id = str(msg.get("id"))
            content = msg.get("content", "")

            # Keyword filtering
            content_lower = content.lower()
            matched = any(kw in content_lower for kw in keywords) if keywords else True
            if not matched:
                continue

            author_data = msg.get("author", {})
            author_name = author_data.get("username", "unknown_user")

            created_at = msg.get("timestamp", current_iso_time())
            reactions = msg.get("reactions", [])
            total_reactions = sum(r.get("count", 0) for r in reactions)

            origin_url = self._format_discord_url(guild_id, channel_id, msg_id)

            item = FeedbackItem(
                id=f"discord:{channel_id}:{msg_id}",
                platform="discord",
                content=content,
                author=author_name,
                created_at=created_at,
                metrics={
                    "reactions": total_reactions,
                    "reaction_details": reactions,
                },
                url=origin_url,
                source_target=channel_id,
                raw_data=msg,
                collected_at=current_iso_time(),
            )
            items.append(item)

            # Track highest watermark
            if newest_item_id is None or int(msg_id) > int(newest_item_id):
                newest_item_id = msg_id
                newest_timestamp = created_at

        # 4. Update checkpoint watermark
        if newest_item_id != last_item_id:
            self.checkpoint_mgr.update_checkpoint(
                checkpoint_key=checkpoint_key,
                last_item_id=newest_item_id,
                last_timestamp=newest_timestamp,
                increment_count=len(items),
            )
            self.logger.info(
                f"[discord] Watermark updated to {newest_item_id} (collected {len(items)} items)."
            )

        return items

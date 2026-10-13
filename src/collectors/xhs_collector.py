from __future__ import annotations

import asyncio
import os
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.collectors.base import BaseCollector
from src.models.schema import FeedbackItem, current_iso_time
from src.utils.retry import async_random_sleep, retry_with_backoff


class XHSCollector(BaseCollector):
    """
    Xiaohongshu (XHS) Public Note Collector via Playwright.
    Engineered for:
    1. Session persistence via Playwright storage_state (avoids re-logging in)
    2. Respectful rate limiting with random delays (2-5s)
    3. Incremental scraping and watermark tracking
    4. Compliance-first: Halts gracefully upon anti-bot challenge (no CAPTCHA bypassing)
    5. Fallback simulation mode for testing environments
    """

    @property
    def platform_name(self) -> str:
        return "xhs"

    def get_targets(self) -> List[str]:
        return self.config.xhs_search_keywords

    def _format_note_url(self, note_id: str) -> str:
        return f"https://www.xiaohongshu.com/explore/{note_id}"

    async def _extract_with_playwright(self, keyword: str, last_item_id: Optional[str]) -> List[FeedbackItem]:
        """
        Use Playwright to query XHS search page.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.logger.warning(
                "[xhs] Playwright package is not installed. To run real browser automation, run:\n"
                "  pip install playwright && playwright install chromium\n"
                "Switching to simulated sample mode for current run."
            )
            return self._generate_mock_notes(keyword, last_item_id)

        items: List[FeedbackItem] = []
        state_path = Path(self.config.xhs_storage_state_path)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=self.config.xhs_headless,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )

            context_kwargs: Dict[str, Any] = {
                "user_agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
                "viewport": {"width": 1280, "height": 800},
            }
            if state_path.exists():
                self.logger.info(f"[xhs] Using existing storage_state from: {state_path}")
                context_kwargs["storage_state"] = str(state_path)

            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()

            try:
                search_url = f"https://www.xiaohongshu.com/search_result?keyword={urllib.parse.quote(keyword)}"
                self.logger.info(f"[xhs] Navigating to: {search_url}")
                await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)

                # Rate limiting delay
                await async_random_sleep(self.config.min_delay_seconds, self.config.max_delay_seconds)

                # Compliance Check: Detect verification or blocking
                page_content = await page.content()
                if "验证码" in page_content or "安全验证" in page_content:
                    self.logger.warning(
                        "[xhs] COMPLIANCE NOTICE: Verification challenge detected. "
                        "Halting scrape to respect platform policy. Please update auth session."
                    )
                    return items

                # Extract note elements
                note_cards = await page.query_selector_all(".note-item, section.note-item, a[href*='/explore/']")
                self.logger.info(f"[xhs] Found {len(note_cards)} note candidate elements on page.")

                for card in note_cards[: self.config.xhs_max_notes_per_run]:
                    href = await card.get_attribute("href")
                    if not href or "/explore/" not in href:
                        # Try finding anchor inside
                        anchor = await card.query_selector("a[href*='/explore/']")
                        if anchor:
                            href = await anchor.get_attribute("href")

                    if not href:
                        continue

                    # Extract note ID
                    note_id = href.split("/explore/")[-1].split("?")[0].strip()
                    if not note_id:
                        continue

                    # Incremental check: if we hit the watermark note, we can stop
                    if last_item_id and note_id == last_item_id:
                        self.logger.info(f"[xhs] Reached checkpoint watermark note_id {note_id}. Stopping crawl.")
                        break

                    # Extract text content and author
                    text_content = (await card.inner_text()).strip()
                    lines = [line.strip() for line in text_content.split("\n") if line.strip()]
                    title_content = " | ".join(lines[:3]) if lines else f"XHS post {note_id}"
                    author = lines[-1] if len(lines) > 1 else "xhs_user"

                    origin_url = self._format_note_url(note_id)

                    item = FeedbackItem(
                        id=f"xhs:{note_id}",
                        platform="xhs",
                        content=title_content,
                        author=author,
                        created_at=current_iso_time(),
                        metrics={"likes": 0, "comments": 0},
                        url=origin_url,
                        source_target=keyword,
                        raw_data={"href": href, "raw_preview": text_content[:200]},
                        collected_at=current_iso_time(),
                    )
                    items.append(item)

                # Save updated storage state if logged in
                if not state_path.exists() and not self.config.xhs_headless:
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    await context.storage_state(path=str(state_path))
                    self.logger.info(f"[xhs] Saved session storage state to {state_path}")

            finally:
                await context.close()
                await browser.close()

        return items

    def _generate_mock_notes(self, keyword: str, last_item_id: Optional[str]) -> List[FeedbackItem]:
        """Realistic simulated notes for testing, development, and offline evaluation."""
        mock_data = [
            {
                "note_id": "64a000000000000001",
                "title": f"【吐槽】用这个{keyword}导出的图表中文全都乱码了，官方有人管管吗？",
                "author": "小红薯_科技迷",
                "created_at": "2026-10-11T12:00:00Z",
                "metrics": {"likes": 45, "comments": 18, "collects": 7},
            },
            {
                "note_id": "64a000000000000002",
                "title": f"建议给{keyword}增加一个批量撤销功能，误删一条整组数据全没了太崩溃了！",
                "author": "产品经理小张",
                "created_at": "2026-10-12T15:20:00Z",
                "metrics": {"likes": 128, "comments": 34, "collects": 56},
            },
            {
                "note_id": "64a000000000000003",
                "title": f"深度测评{keyword}：响应速度比上个版本提升了2倍，但是移动端UI排版有点挤。",
                "author": "数码达人Leo",
                "created_at": "2026-10-13T09:40:00Z",
                "metrics": {"likes": 89, "comments": 12, "collects": 40},
            },
        ]

        items: List[FeedbackItem] = []
        for d in mock_data:
            if last_item_id and d["note_id"] == last_item_id:
                break
            items.append(
                FeedbackItem(
                    id=f"xhs:{d['note_id']}",
                    platform="xhs",
                    content=d["title"],
                    author=d["author"],
                    created_at=d["created_at"],
                    metrics=d["metrics"],
                    url=self._format_note_url(d["note_id"]),
                    source_target=keyword,
                    raw_data=d,
                    collected_at=current_iso_time(),
                )
            )
        return items

    async def fetch_target_items(self, target: str) -> List[FeedbackItem]:
        keyword = target
        checkpoint_key = f"xhs:{keyword}"

        # 1. Read watermark
        last_item_id, last_timestamp, _ = self.checkpoint_mgr.get_checkpoint(checkpoint_key)
        self.logger.info(
            f"[xhs] Watermark for keyword '{keyword}': last_item_id={last_item_id}"
        )

        # 2. Extract items
        items = await self._extract_with_playwright(keyword=keyword, last_item_id=last_item_id)

        # 3. Update watermark
        if items:
            newest_item = items[0]  # First item is newest in search page
            raw_id = newest_item.id.replace("xhs:", "")
            self.checkpoint_mgr.update_checkpoint(
                checkpoint_key=checkpoint_key,
                last_item_id=raw_id,
                last_timestamp=newest_item.created_at,
                increment_count=len(items),
            )
            self.logger.info(
                f"[xhs] Watermark updated for '{keyword}' to {raw_id} (collected {len(items)} items)."
            )

        return items

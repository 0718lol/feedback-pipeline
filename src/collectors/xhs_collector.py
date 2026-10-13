"""
Xiaohongshu (小红书) public search collector, driven by Playwright.

Compliance stance (non-negotiable, see README):
  * Only publicly reachable search-result pages are read.
  * The operator logs in manually once; the resulting session is persisted via
    Playwright `storage_state` and reused. Credentials never touch this codebase.
  * Verification / captcha challenges cause an immediate, loud stop. There is no
    bypass, no solver service and no proxy rotation anywhere in this project.
  * Requests are spaced by a random jitter delay and bounded by an explicit cap.

Engineering stance:
  * This collector never fabricates data. If it cannot reach a logged-in search
    page it raises a typed error, which the pipeline records as a real failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set

from src.collectors.base import BaseCollector
from src.errors import (
    CollectorConfigError,
    LoginRequiredError,
    PlatformBlockedError,
)
from src.models.schema import FeedbackItem, current_iso_time
from src.utils.retry import async_random_sleep
from src.utils.timeparse import parse_xhs_time

SEARCH_URL_TEMPLATE = "https://www.xiaohongshu.com/search_result?keyword={kw}"

# Selectors are intentionally layered: Xiaohongshu ships DOM changes regularly, and a
# single hard-coded class name is the most common cause of silent zero-result runs.
NOTE_CARD_SELECTORS = (
    "section.note-item",
    "div.note-item",
    "[class*='note-item']",
)

LOGIN_WALL_MARKERS = ("登录后查看搜索结果", "登录后查看更多", "扫码登录")
BLOCK_MARKERS = ("安全验证", "请完成验证", "滑块验证", "captcha")

# Matches /explore/<id>, /search_result/<id> and /discovery/item/<id> alike.
NOTE_ID_RE = re.compile(r"/(?:explore|search_result|discovery/item)/([0-9a-fA-F]{16,32})")

# Real Xiaohongshu note ids are 24 lowercase hex characters. Promoted / sponsored
# slots reuse the same `note-item` markup but carry UUID-style ids such as
# "4c3d806d-4209-4dfa-b13c-4e3e9d0f94ad#1791896989117", which we must reject --
# they are advertisements, not user notes, and they carry no publish timestamp.
NOTE_ID_STRICT_RE = re.compile(r"^[0-9a-f]{24}$")


def parse_interaction_count(raw: str) -> int:
    """
    Parse a Xiaohongshu interaction counter such as '1.2万', '3,204' or '45' into an int.
    Returns 0 when nothing numeric can be recovered.
    """
    if not raw:
        return 0
    text = str(raw).strip().replace(",", "").replace(" ", "")
    if not text:
        return 0
    multiplier = 1
    if text.endswith("万"):
        multiplier, text = 10_000, text[:-1]
    elif text.endswith("亿"):
        multiplier, text = 100_000_000, text[:-1]
    elif text[-1:] in ("w", "W"):
        multiplier, text = 10_000, text[:-1]
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return 0
    try:
        return int(float(match.group(1)) * multiplier)
    except (TypeError, ValueError):
        return 0


class XHSCollector(BaseCollector):
    """Playwright-based collector for Xiaohongshu public search results."""

    @property
    def platform_name(self) -> str:
        return "xhs"

    def get_targets(self) -> List[str]:
        return list(self.config.xhs_search_keywords)

    @property
    def storage_state_path(self) -> Path:
        return Path(self.config.xhs_storage_state_path)

    def has_saved_session(self) -> bool:
        """True when a storage_state file exists and actually carries cookies."""
        path = self.storage_state_path
        if not path.exists() or path.stat().st_size == 0:
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(data.get("cookies"))

    # ------------------------------------------------------------------ browser

    @contextlib.asynccontextmanager
    async def _browser_context(self, headless: bool, use_saved_session: bool = True):
        """
        Yield a Playwright (context, page) pair.

        Prefers the locally installed Google Chrome via `channel`, which avoids the
        ~150MB `playwright install chromium` download. Falls back to a bundled
        Chromium if that channel is unavailable.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise CollectorConfigError(
                "Playwright is not installed. Run: pip install -r requirements.txt"
            ) from exc

        async with async_playwright() as pw:
            launch_kwargs: Dict[str, Any] = {
                "headless": headless,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            channel = (self.config.xhs_browser_channel or "").strip()
            if channel:
                launch_kwargs["channel"] = channel

            browser = None
            try:
                browser = await pw.chromium.launch(**launch_kwargs)
            except Exception as exc:
                if channel:
                    self.logger.warning(
                        f"[xhs] Browser channel '{channel}' unavailable; "
                        "falling back to the bundled Chromium build."
                    )
                    launch_kwargs.pop("channel", None)
                    try:
                        browser = await pw.chromium.launch(**launch_kwargs)
                    except Exception as inner:
                        raise CollectorConfigError(
                            "No usable browser found. Either install Google Chrome, or run "
                            "`python -m playwright install chromium`."
                        ) from inner
                else:
                    raise CollectorConfigError(
                        "No usable browser found. Either install Google Chrome, or run "
                        "`python -m playwright install chromium`."
                    ) from exc

            context_kwargs: Dict[str, Any] = {
                "user_agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
                "viewport": {"width": 1440, "height": 900},
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
            }
            if use_saved_session and self.has_saved_session():
                context_kwargs["storage_state"] = str(self.storage_state_path)
                self.logger.info(f"[xhs] Reusing saved session: {self.storage_state_path}")

            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()
            try:
                yield context, page
            finally:
                with contextlib.suppress(Exception):
                    await context.close()
                with contextlib.suppress(Exception):
                    await browser.close()

    # -------------------------------------------------------------------- login

    async def interactive_login(self) -> Path:
        """
        Open a visible browser so the operator can log in by QR code / SMS, then
        persist the session to `xhs_storage_state_path`.

        This is the ONLY step that requires a human. It is deliberately manual:
        the project does not automate credential entry.
        """
        self.logger.info(
            "[xhs] Opening a visible Chrome window. Scan the QR code with the "
            "Xiaohongshu app. Waiting up to "
            f"{self.config.xhs_login_timeout_seconds}s for a successful login..."
        )

        async with self._browser_context(headless=False, use_saved_session=False) as (context, page):
            await page.goto(
                "https://www.xiaohongshu.com/explore",
                wait_until="domcontentloaded",
                timeout=60_000,
            )

            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.config.xhs_login_timeout_seconds
            logged_in = False
            while loop.time() < deadline:
                if await self._detect_page_state(page) == "ready":
                    logged_in = True
                    break
                await asyncio.sleep(2.0)

            if not logged_in:
                raise LoginRequiredError(
                    "Login was not completed within "
                    f"{self.config.xhs_login_timeout_seconds}s. "
                    "Re-run `python main.py login`."
                )

            self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(self.storage_state_path))
            self.logger.info(f"[xhs] Session saved to {self.storage_state_path}")
            return self.storage_state_path

    # --------------------------------------------------------------- inspection

    async def _detect_page_state(self, page) -> str:
        """Classify the current page: 'ready' | 'login_required' | 'blocked' | 'unknown'."""
        try:
            body_text = await page.evaluate("document.body ? document.body.innerText : ''")
        except Exception:
            body_text = ""
        body_text = body_text or ""

        if any(marker in body_text for marker in BLOCK_MARKERS):
            return "blocked"

        for selector in NOTE_CARD_SELECTORS:
            try:
                if await page.locator(selector).count():
                    return "ready"
            except Exception:
                continue

        if any(marker in body_text for marker in LOGIN_WALL_MARKERS):
            return "login_required"

        for selector in (".login-container", ".login-modal"):
            try:
                if await page.locator(selector).count():
                    return "login_required"
            except Exception:
                continue
        return "unknown"

    async def dump_dom_diagnostics(self, keyword: str) -> Dict[str, Any]:
        """
        Development helper: report what the search page actually looks like right now.
        Use it to re-tune selectors when Xiaohongshu ships a DOM change.
        """
        async with self._browser_context(headless=self.config.xhs_headless) as (context, page):
            url = SEARCH_URL_TEMPLATE.format(kw=urllib.parse.quote(keyword))
            response = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(6000)
            state = await self._detect_page_state(page)

            selector_counts: Dict[str, int] = {}
            for selector in NOTE_CARD_SELECTORS + (
                "a.cover",
                "a[href*='/explore/']",
                "a[href*='/search_result/']",
            ):
                try:
                    selector_counts[selector] = await page.locator(selector).count()
                except Exception:
                    selector_counts[selector] = -1

            class_histogram = await page.evaluate(
                """() => {
                    const out = {};
                    document.querySelectorAll('*').forEach(el => {
                        const c = (el.className && typeof el.className === 'string') ? el.className : '';
                        if (/note|feed|card|cover/i.test(c)) {
                            c.split(/\\s+/).filter(Boolean).forEach(k => { out[k] = (out[k] || 0) + 1; });
                        }
                    });
                    return Object.entries(out).sort((a, b) => b[1] - a[1]).slice(0, 30);
                }"""
            )
            body_text = await page.evaluate("document.body ? document.body.innerText : ''")

            return {
                "keyword": keyword,
                "http_status": response.status if response else None,
                "final_url": page.url,
                "title": await page.title(),
                "state": state,
                "selector_counts": selector_counts,
                "class_histogram": class_histogram,
                "body_preview": (body_text or "")[:800],
            }

    # ---------------------------------------------------------------- extraction

    async def _extract_cards(self, page, keyword: str, limit: int) -> List[Dict[str, Any]]:
        """
        Read every note card currently mounted in the DOM, de-duplicated by note id.

        Selectors below were verified against the live search page (2026-10). Each
        card is `<section class="note-item" data-note-id="...">`; the publish time
        lives in `.name-time-wrapper .time` as a relative label such as "7小时前".
        """
        raw_cards = await page.evaluate(
            """() => {
                const cards = Array.from(document.querySelectorAll('section.note-item'));
                const text = el => el ? ((el.innerText || el.textContent || '').trim()) : '';
                return cards.map(card => {
                    const cover = card.querySelector('a.cover')
                        || card.querySelector("a[href*='/explore/']")
                        || card.querySelector("a[href*='/search_result/']");
                    const titleEl = card.querySelector('a.title span')
                        || card.querySelector('.title span')
                        || card.querySelector('a.title')
                        || card.querySelector('.title');
                    const authorEl = card.querySelector('.name-time-wrapper .name')
                        || card.querySelector('.name');
                    const timeEl = card.querySelector('.name-time-wrapper .time')
                        || card.querySelector('.time');
                    const likeEl = card.querySelector('.like-wrapper .count')
                        || card.querySelector('.count');
                    const imgEl = card.querySelector('img');
                    return {
                        note_id_attr: card.getAttribute('data-note-id'),
                        href: cover ? cover.getAttribute('href') : null,
                        title: text(titleEl),
                        author: text(authorEl),
                        time_text: text(timeEl),
                        like_text: text(likeEl),
                        cover: imgEl ? imgEl.getAttribute('src') : null,
                        card_text: (card.innerText || '').trim().slice(0, 400),
                    };
                });
            }"""
        )

        notes: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        skipped_promoted = 0
        for card in raw_cards:
            # `data-note-id` is authoritative when it holds a real note id; otherwise
            # fall back to the href, which still points at the canonical note.
            note_id = (card.get("note_id_attr") or "").strip()
            if not NOTE_ID_STRICT_RE.match(note_id):
                match = NOTE_ID_RE.search(card.get("href") or "")
                note_id = match.group(1) if match else ""
            if not NOTE_ID_STRICT_RE.match(note_id):
                skipped_promoted += 1
                continue
            if note_id in seen:
                continue
            seen.add(note_id)
            card["note_id"] = note_id
            card["keyword"] = keyword
            notes.append(card)
            if len(notes) >= limit:
                break

        if skipped_promoted:
            self.logger.info(
                f"[xhs] Skipped {skipped_promoted} promoted/non-note card(s) with "
                "non-canonical ids."
            )
        return notes

    def _card_to_feedback_item(self, card: Dict[str, Any]) -> FeedbackItem:
        note_id = card["note_id"]
        title = (card.get("title") or "").strip()
        card_text = (card.get("card_text") or "").strip()
        author = (card.get("author") or "").strip()
        time_text = (card.get("time_text") or "").strip()

        # Fall back to the card's visible text when the dedicated title node is absent,
        # so `content` is never empty for a genuinely captured note.
        content = title or card_text or f"小红书笔记 {note_id}"

        # Search cards expose a relative publish label ("7小时前", "08-19"), which we
        # resolve to an absolute timestamp. Unparseable labels stay empty so the
        # quality monitor reports the gap instead of fabricating a date.
        created_at = parse_xhs_time(time_text, now=datetime.now(timezone.utc))

        return FeedbackItem(
            id=f"xhs:{note_id}",
            platform="xhs",
            content=content,
            author=author or "未知作者",
            created_at=created_at,
            metrics={"likes": parse_interaction_count(card.get("like_text", ""))},
            url=f"https://www.xiaohongshu.com/explore/{note_id}",
            source_target=card.get("keyword", ""),
            raw_data={
                "href": card.get("href"),
                "cover": card.get("cover"),
                "card_text": card_text,
                "like_text": card.get("like_text"),
                "time_text": time_text,
                "source": "xhs_search_page",
            },
            collected_at=current_iso_time(),
        )

    # --------------------------------------------------------------------- main

    async def fetch_target_items(self, target: str) -> List[FeedbackItem]:
        keyword = target
        checkpoint_key = f"xhs:{keyword}"

        if not self.has_saved_session():
            raise LoginRequiredError(
                f"No saved Xiaohongshu session at '{self.storage_state_path}'. "
                "Run `python main.py login` once, then re-run collection."
            )

        last_item_id, _, _ = self.checkpoint_mgr.get_checkpoint(checkpoint_key)
        already_seen = self.checkpoint_mgr.get_seen_ids(checkpoint_key)
        self.logger.info(
            f"[xhs] Watermark for keyword '{keyword}': last_note_id={last_item_id}, "
            f"previously_seen={len(already_seen)}"
        )

        limit = self.config.xhs_max_notes_per_run
        url = SEARCH_URL_TEMPLATE.format(kw=urllib.parse.quote(keyword))

        async with self._browser_context(headless=self.config.xhs_headless) as (context, page):
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await async_random_sleep(self.config.min_delay_seconds, self.config.max_delay_seconds)

            state = await self._detect_page_state(page)
            if state == "blocked":
                raise PlatformBlockedError(
                    f"Xiaohongshu served a verification challenge for keyword '{keyword}'. "
                    "Halting by policy; no bypass is attempted. Re-run later."
                )
            if state == "login_required":
                raise LoginRequiredError(
                    f"Xiaohongshu requires login for keyword '{keyword}'. The saved session "
                    "may have expired -- run `python main.py login` again."
                )
            if state != "ready":
                self.logger.warning(
                    f"[xhs] Page state '{state}' is ambiguous for keyword '{keyword}'; "
                    "attempting extraction anyway."
                )

            collected: List[Dict[str, Any]] = []
            scanned: Set[str] = set()
            consecutive_known = 0
            stop_reason = "scroll_limit"

            for scroll_index in range(self.config.xhs_max_scrolls):
                cards = await self._extract_cards(page, keyword, limit)
                new_this_pass = 0

                for card in cards:
                    note_id = card["note_id"]
                    if note_id in scanned:
                        continue
                    scanned.add(note_id)

                    if note_id in already_seen:
                        # Already persisted by an earlier run: this is the incremental
                        # frontier. We do not re-emit it, but we keep counting how many
                        # known notes we have walked past.
                        consecutive_known += 1
                        continue

                    consecutive_known = 0
                    collected.append(card)
                    new_this_pass += 1

                self.logger.info(
                    f"[xhs] scroll {scroll_index + 1}/{self.config.xhs_max_scrolls}: "
                    f"{len(cards)} cards mounted, {new_this_pass} new, "
                    f"{consecutive_known} consecutive already-known."
                )

                if len(collected) >= limit:
                    stop_reason = "limit_reached"
                    break
                if consecutive_known >= self.config.xhs_stop_after_seen:
                    # Search results are relevance-ordered, not time-ordered, so
                    # "first known id == frontier" would be wrong. We instead stop once
                    # a long run of results is entirely already-known, which is the
                    # standard convergence heuristic for paginated relevance feeds.
                    stop_reason = "converged"
                    break

                await page.mouse.wheel(0, 2400)
                await async_random_sleep(
                    self.config.min_delay_seconds, self.config.max_delay_seconds
                )

            self.logger.info(
                f"[xhs] Finished '{keyword}': {len(collected)} new notes "
                f"(stop_reason={stop_reason}, scanned={len(scanned)})."
            )

        return [self._card_to_feedback_item(card) for card in collected]

    def commit_checkpoint(self, target: str, items: List[FeedbackItem]) -> None:
        """
        Advance the watermark and seen-set for `target`.

        Called by BaseCollector.run() *after* the items have been persisted, never
        before. If the process dies mid-run, nothing has been marked as seen, so the
        next run re-fetches those notes instead of silently skipping them forever.
        """
        if not items:
            return

        checkpoint_key = f"xhs:{target}"
        note_ids = [item.id.replace("xhs:", "") for item in items]

        self.checkpoint_mgr.update_checkpoint(
            checkpoint_key=checkpoint_key,
            last_item_id=note_ids[-1],
            last_timestamp=items[-1].collected_at,
            increment_count=len(items),
        )
        total_seen = self.checkpoint_mgr.add_seen_ids(checkpoint_key, note_ids)
        self.logger.info(
            f"[xhs] Watermark advanced for '{target}' to note {note_ids[-1]} "
            f"({len(items)} new notes committed, {total_seen} ids tracked)."
        )

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


def load_env_file(dotenv_path: str = ".env") -> None:
    """Minimal native .env loader without requiring external python-dotenv package."""
    path = Path(dotenv_path)
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k and k not in os.environ:
                os.environ[k] = v


@dataclass
class AppConfig:
    # Database
    database_url: str = "sqlite:///data/feedbacks.db"

    # LLM
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"

    # Discord
    discord_bot_token: str = ""
    discord_target_guild_ids: List[str] = field(default_factory=list)
    discord_target_channel_ids: List[str] = field(default_factory=list)
    discord_keywords: List[str] = field(default_factory=lambda: ["bug", "crash", "feedback", "feature request", "建议", "报错"])

    # XHS
    xhs_search_keywords: List[str] = field(default_factory=lambda: ["AI工具", "数据科学", "用户体验反馈"])
    xhs_headless: bool = True
    xhs_storage_state_path: str = "data/xhs_auth_state.json"
    xhs_max_notes_per_run: int = 50
    # Which browser Playwright should drive. "chrome" reuses the locally installed
    # Google Chrome, so no `playwright install` download is required.
    xhs_browser_channel: str = "chrome"
    # Seconds to wait for a human to finish scanning the login QR code.
    xhs_login_timeout_seconds: int = 300
    # Max scrolls per keyword while paging through search results.
    xhs_max_scrolls: int = 12
    # Stop paging after this many consecutive already-seen notes (incremental convergence).
    xhs_stop_after_seen: int = 20

    # Rate Limit & Delays
    min_delay_seconds: float = 2.0
    max_delay_seconds: float = 5.0
    max_retries: int = 3

    @classmethod
    def load(cls) -> "AppConfig":
        load_env_file()

        def split_env(key: str) -> List[str]:
            val = os.environ.get(key, "").strip()
            if not val:
                return []
            return [x.strip() for x in val.split(",") if x.strip()]

        guilds = split_env("DISCORD_TARGET_GUILD_IDS")
        channels = split_env("DISCORD_TARGET_CHANNEL_IDS")
        discord_kw = split_env("DISCORD_KEYWORDS") or ["bug", "crash", "feedback", "feature request", "建议", "报错"]
        xhs_kw = split_env("XHS_SEARCH_KEYWORDS") or ["AI工具", "数据科学", "用户体验反馈"]

        return cls(
            database_url=os.environ.get("DATABASE_URL", "sqlite:///data/feedbacks.db"),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            openai_base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            llm_model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
            discord_bot_token=os.environ.get("DISCORD_BOT_TOKEN", ""),
            discord_target_guild_ids=guilds,
            discord_target_channel_ids=channels,
            discord_keywords=discord_kw,
            xhs_search_keywords=xhs_kw,
            xhs_headless=os.environ.get("XHS_HEADLESS", "true").lower() in ("true", "1", "yes"),
            xhs_storage_state_path=os.environ.get("XHS_STORAGE_STATE_PATH", "data/xhs_auth_state.json"),
            xhs_max_notes_per_run=int(os.environ.get("XHS_MAX_NOTES_PER_RUN", "50")),
            xhs_browser_channel=os.environ.get("XHS_BROWSER_CHANNEL", "chrome"),
            xhs_login_timeout_seconds=int(os.environ.get("XHS_LOGIN_TIMEOUT_SECONDS", "300")),
            xhs_max_scrolls=int(os.environ.get("XHS_MAX_SCROLLS", "12")),
            xhs_stop_after_seen=int(os.environ.get("XHS_STOP_AFTER_SEEN", "20")),
            min_delay_seconds=float(os.environ.get("COLLECTOR_MIN_DELAY_SECONDS", "2.0")),
            max_delay_seconds=float(os.environ.get("COLLECTOR_MAX_DELAY_SECONDS", "5.0")),
            max_retries=int(os.environ.get("COLLECTOR_MAX_RETRIES", "3")),
        )

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional


DEFAULT_DB_PATH = Path("data/feedbacks.db")


def get_db_connection(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    """
    Open SQLite database connection with row factory, foreign keys, and WAL journal mode.
    """
    if db_path is None:
        env_url = os.environ.get("DATABASE_URL", "")
        if env_url.startswith("sqlite:///"):
            db_path = Path(env_url.replace("sqlite:///", ""))
        else:
            db_path = DEFAULT_DB_PATH

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row

    # Performance & Concurrency settings
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def init_db(db_path: Optional[str | Path] = None) -> None:
    """
    Initialize all database tables, indexes, and constraints.
    Safe to run repeatedly (Idempotent schema creation).
    """
    conn = get_db_connection(db_path)
    with conn:
        # 1. Main Feedback table (Primary Key enforces Deduplication & Idempotency)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feedbacks (
                id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                content TEXT NOT NULL,
                author TEXT NOT NULL,
                created_at TEXT NOT NULL,
                metrics TEXT NOT NULL,
                url TEXT NOT NULL,
                source_target TEXT NOT NULL,
                raw_data TEXT NOT NULL,
                collected_at TEXT NOT NULL
            );
        """)

        # Indexes for fast incremental lookups, timeline sorting, and platform filtering
        conn.execute("CREATE INDEX IF NOT EXISTS idx_feedbacks_platform ON feedbacks(platform);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_feedbacks_created_at ON feedbacks(created_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_feedbacks_target ON feedbacks(source_target);")

        # 2. Checkpoints table (Supports Watermarking & Breakpoint Resume)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                checkpoint_key TEXT PRIMARY KEY,
                last_item_id TEXT,
                last_timestamp TEXT,
                items_count INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
            );
        """)

        # 3. LLM Analysis Results (Foreign Key with Cascade to Feedbacks)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS analysis_results (
                feedback_id TEXT PRIMARY KEY,
                sentiment TEXT NOT NULL,
                category TEXT NOT NULL,
                summary TEXT NOT NULL,
                confidence REAL NOT NULL,
                model TEXT NOT NULL,
                analyzed_at TEXT NOT NULL,
                FOREIGN KEY (feedback_id) REFERENCES feedbacks(id) ON DELETE CASCADE
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_analysis_category ON analysis_results(category);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_analysis_sentiment ON analysis_results(sentiment);")

        # 4. Collector Run Logs (Telemetry, Quality Monitoring, Alerting)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS collection_runs (
                run_id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                target TEXT NOT NULL,
                status TEXT NOT NULL,
                items_collected INTEGER DEFAULT 0,
                items_new INTEGER DEFAULT 0,
                items_failed INTEGER DEFAULT 0,
                start_time TEXT NOT NULL,
                end_time TEXT,
                error_message TEXT
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_platform ON collection_runs(platform);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_start_time ON collection_runs(start_time);")

    conn.close()

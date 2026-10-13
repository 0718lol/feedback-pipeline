from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional


DEFAULT_DB_PATH = Path("data/feedbacks.db")


def resolve_db_path(database_url: Optional[str] = None) -> Path:
    """
    Resolve a SQLAlchemy-style SQLite URL into a concrete filesystem path.

    Accepted forms:
        sqlite:///data/feedbacks.db     -> relative path  data/feedbacks.db
        sqlite:////tmp/feedbacks.db     -> absolute path  /tmp/feedbacks.db
        sqlite:///:memory:              -> in-memory database
        /plain/path.db  or  path.db     -> used verbatim

    Resolution order when `database_url` is None:
        1. DATABASE_URL environment variable
        2. DEFAULT_DB_PATH
    """
    url = (database_url or os.environ.get("DATABASE_URL") or "").strip()

    if not url:
        return DEFAULT_DB_PATH

    if url in ("sqlite://", "sqlite:///:memory:", ":memory:"):
        return Path(":memory:")

    if url.startswith("sqlite:///"):
        # sqlite:///data/x.db  -> "data/x.db"    (relative)
        # sqlite:////tmp/x.db  -> "/tmp/x.db"    (absolute, four slashes)
        return Path(url[len("sqlite:///"):])

    if "://" in url:
        raise ValueError(
            f"Unsupported database URL '{url}'. This project persists to SQLite only; "
            "use sqlite:///relative/path.db or sqlite:////absolute/path.db."
        )

    return Path(url)


def get_db_connection(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    """
    Open SQLite database connection with row factory, foreign keys, and WAL journal mode.
    """
    path = resolve_db_path(str(db_path) if db_path is not None else None)

    if str(path) == ":memory:":
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row

    # Performance & Concurrency settings
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """
    Create all tables, indexes and constraints on an already-open connection.
    Safe to run repeatedly (idempotent DDL). Split out from init_db so that
    in-memory databases can keep using the same connection they were built on.
    """
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
                seen_ids TEXT DEFAULT '[]',
                updated_at TEXT NOT NULL
            );
        """)
        # Lightweight forward migration for databases created before `seen_ids` existed.
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(checkpoints);")}
        if "seen_ids" not in existing_columns:
            conn.execute("ALTER TABLE checkpoints ADD COLUMN seen_ids TEXT DEFAULT '[]';")

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


def init_db(db_path: Optional[str | Path] = None) -> None:
    """
    Initialize schema on a file-backed database, then close the connection.
    Not suitable for ':memory:' (the schema would vanish with the connection) --
    use open_database() for that.
    """
    conn = get_db_connection(db_path)
    try:
        create_schema(conn)
    finally:
        conn.close()


def open_database(database_url: Optional[str] = None) -> sqlite3.Connection:
    """
    Open a connection and guarantee the schema exists on *that same* connection.
    This is the correct entry point for both file-backed and in-memory databases.
    """
    conn = get_db_connection(resolve_db_path(database_url))
    create_schema(conn)
    return conn

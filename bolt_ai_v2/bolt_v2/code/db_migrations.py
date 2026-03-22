"""
Bolt AI -- Database Migration System (db_migrations.py)
========================================================
Simple version-based migration system for SQLite schema changes.

Uses PRAGMA user_version to track which migrations have been applied.
Each migration is a function that receives a connection and applies
schema changes. Migrations are idempotent and applied in order.

Usage:
    from db_migrations import run_migrations
    run_migrations(db_path)  # called once at startup in database.py
"""

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger("bolt.migrations")


# ── Migration functions ────────────────────────────────────────────────────
# Each migration bumps the schema to the next version.
# Migrations MUST be idempotent (safe to run multiple times).

def _migration_001_add_publication_metrics(conn: sqlite3.Connection) -> None:
    """Add views and engagement_rate columns to publications table."""
    # These columns may already exist from the initial schema -- use try/except
    for col, typedef in [("views", "INTEGER DEFAULT 0"), ("engagement_rate", "REAL DEFAULT 0")]:
        try:
            conn.execute(f"ALTER TABLE publications ADD COLUMN {col} {typedef}")
            logger.info(f"Migration 001: added publications.{col}")
        except sqlite3.OperationalError:
            pass  # Column already exists


def _migration_002_add_dead_letters_table(conn: sqlite3.Connection) -> None:
    """Add dead_letters table for exhausted retry jobs."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dead_letters (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id        INTEGER,
            job_type      TEXT NOT NULL,
            content_id    TEXT,
            error_msg     TEXT,
            attempts      INTEGER DEFAULT 0,
            dead_at       TEXT NOT NULL
        )
    """)
    logger.info("Migration 002: dead_letters table ensured")


def _migration_003_add_article_hashes_table(conn: sqlite3.Connection) -> None:
    """Add article_hashes table for persistent deduplication."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS article_hashes (
            hash       TEXT PRIMARY KEY,
            title      TEXT NOT NULL,
            source     TEXT,
            first_seen TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_article_hashes_seen ON article_hashes(first_seen)"
    )
    logger.info("Migration 003: article_hashes table ensured")


def _migration_004_add_performance_overrides(conn: sqlite3.Connection) -> None:
    """Add performance_overrides table for the feedback aggregator."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS performance_overrides (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            key        TEXT UNIQUE NOT NULL,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    logger.info("Migration 004: performance_overrides table created")


# ── Migration registry ─────────────────────────────────────────────────────
# Order matters. Each entry is (version_number, function).
MIGRATIONS = [
    (1, _migration_001_add_publication_metrics),
    (2, _migration_002_add_dead_letters_table),
    (3, _migration_003_add_article_hashes_table),
    (4, _migration_004_add_performance_overrides),
]


def run_migrations(db_path: "str | Path") -> int:
    """Apply any pending migrations to the database.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Number of migrations applied.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")

    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    applied = 0

    for version, migrate_fn in MIGRATIONS:
        if version > current_version:
            logger.info(f"Applying migration {version}: {migrate_fn.__doc__.strip()}")
            try:
                migrate_fn(conn)
                conn.execute(f"PRAGMA user_version = {version}")
                conn.commit()
                applied += 1
            except Exception as e:
                conn.rollback()
                logger.error(f"Migration {version} failed: {e}")
                raise

    if applied:
        logger.info(f"Applied {applied} migration(s), schema now at version {MIGRATIONS[-1][0]}")
    else:
        logger.debug(f"Database schema up to date (version {current_version})")

    conn.close()
    return applied

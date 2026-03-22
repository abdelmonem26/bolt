#!/usr/bin/env python3
"""
Bolt AI — Database Layer (database.py)
=======================================
Replaces scattered JSON files with a single SQLite database.
SQLite is the right choice here — no server, no config, 
file-based, handles concurrent reads fine, and is production-ready
for a pipeline that produces 1-3 videos/day.

If you need to scale beyond ~100 videos/day in the future,
migrating to PostgreSQL requires only changing the connection string.

Tables:
  articles     — news articles fetched from RSS feeds
  scripts      — generated scripts with quality scores
  videos       — video pipeline output (audio, avatar, final paths)
  publications — per-platform publish results
  analytics    — daily platform metric snapshots
  cost_events  — individual API cost tracking events
  jobs         — retry queue for failed pipeline jobs

Usage:
  from database import get_db, Article, Script, Video, Publication

  db = get_db()
  db.save_article(article_dict)
  script = db.get_pending_script()
  db.save_publication(content_id, "youtube", success=True, url="...")
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("bolt.database")

DEFAULT_DB_PATH = Path("data/bolt.db")


# ── Schema ─────────────────────────────────────────────────────────────────

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS articles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    title         TEXT NOT NULL,
    summary       TEXT,
    link          TEXT,
    pillar        TEXT,
    claude_score  REAL DEFAULT 0,
    heuristic_score REAL DEFAULT 0,
    age_hours     REAL DEFAULT 0,
    published_iso TEXT,
    fetched_at    TEXT NOT NULL,
    status        TEXT DEFAULT 'pending'   -- pending | used | skipped
);

CREATE TABLE IF NOT EXISTS scripts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id    TEXT UNIQUE NOT NULL,
    article_id    INTEGER REFERENCES articles(id),
    pillar        TEXT,
    script        TEXT NOT NULL,
    word_count    INTEGER,
    overall_score REAL,
    hook_strength REAL,
    simplicity    REAL,
    bolt_voice    REAL,
    pacing        REAL,
    quality_json  TEXT,      -- full quality dict as JSON
    captions_json TEXT,      -- platform captions as JSON
    status        TEXT DEFAULT 'pending_review',
    auto_approved INTEGER DEFAULT 0,
    review_decision TEXT,
    generated_at  TEXT NOT NULL,
    approved_at   TEXT
);

CREATE TABLE IF NOT EXISTS videos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id      TEXT UNIQUE NOT NULL REFERENCES scripts(content_id),
    audio_path      TEXT,
    audio_provider  TEXT,    -- edge_tts | google_tts | elevenlabs
    avatar_path     TEXT,
    avatar_provider TEXT,    -- vidnoz | did | none
    final_path      TEXT,
    thumbnail_path  TEXT,
    video_ready     INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'pending',
    completed_at    TEXT
);

CREATE TABLE IF NOT EXISTS publications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id  TEXT NOT NULL REFERENCES scripts(content_id),
    platform    TEXT NOT NULL,   -- youtube | tiktok | instagram
    success     INTEGER DEFAULT 0,
    post_url    TEXT,
    post_id     TEXT,
    error_msg   TEXT,
    scheduled_at TEXT,
    published_at TEXT NOT NULL,
    views       INTEGER DEFAULT 0,       -- populated 24h later by analytics_tracker
    engagement_rate REAL DEFAULT 0,      -- populated 24h later by analytics_tracker
    UNIQUE(content_id, platform)
);

CREATE TABLE IF NOT EXISTS dead_letters (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER,
    job_type      TEXT NOT NULL,
    content_id    TEXT,
    error_msg     TEXT,
    attempts      INTEGER DEFAULT 0,
    dead_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analytics_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at    TEXT NOT NULL,
    platform      TEXT NOT NULL,  -- youtube | tiktok | instagram
    followers     INTEGER DEFAULT 0,
    views_30d     INTEGER DEFAULT 0,
    engagement_rate REAL DEFAULT 0,
    video_count   INTEGER DEFAULT 0,
    raw_json      TEXT   -- full platform response as JSON
);

CREATE TABLE IF NOT EXISTS cost_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    service     TEXT NOT NULL,   -- claude | elevenlabs | edge_tts | vidnoz | etc.
    operation   TEXT,
    quantity    REAL,
    cost_usd    REAL DEFAULT 0,
    model       TEXT,
    content_id  TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type     TEXT NOT NULL,  -- news | script | video | publish | analytics
    content_id   TEXT,
    status       TEXT DEFAULT 'pending',  -- pending | running | done | failed | retrying
    attempts     INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    next_run_at  TEXT,
    error_msg    TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Persistent article deduplication across pipeline runs
CREATE TABLE IF NOT EXISTS article_hashes (
    hash       TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    source     TEXT,
    first_seen TEXT NOT NULL
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_scripts_status   ON scripts(status);
CREATE INDEX IF NOT EXISTS idx_scripts_score    ON scripts(overall_score);
CREATE INDEX IF NOT EXISTS idx_publications_pid ON publications(content_id, platform);
CREATE INDEX IF NOT EXISTS idx_cost_service     ON cost_events(service, recorded_at);
CREATE INDEX IF NOT EXISTS idx_jobs_status      ON jobs(status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_articles_status  ON articles(status, fetched_at);
CREATE INDEX IF NOT EXISTS idx_article_hashes_seen ON article_hashes(first_seen);
"""


# ── Connection manager ─────────────────────────────────────────────────────

@contextmanager
def _get_conn(db_path: Path):
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Database class ────────────────────────────────────────────────────────

class BoltDB:
    """Main database interface for the Bolt pipeline."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        # Run any pending migrations after initial schema
        try:
            from db_migrations import run_migrations
            run_migrations(self.db_path)
        except Exception as e:
            logger.warning(f"Migration check failed (non-fatal): {e}")
        logger.info(f"Database ready: {self.db_path}")

    def _init_schema(self) -> None:
        with _get_conn(self.db_path) as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _get_conn_ctx(self):
        """Context manager for raw SQL queries. Properly closes connections."""
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Articles ─────────────────────────────────────────────────────────

    def save_article(self, article: dict) -> int:
        """Save a news article. Returns the row ID."""
        now = datetime.now(timezone.utc).isoformat()
        with _get_conn(self.db_path) as conn:
            cur = conn.execute("""
                INSERT OR IGNORE INTO articles
                  (source, title, summary, link, pillar, claude_score,
                   heuristic_score, age_hours, published_iso, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                article.get("source", ""),
                article.get("title", ""),
                article.get("summary", "")[:1000],
                article.get("link", ""),
                article.get("content_pillar", article.get("pillar", "")),
                article.get("claude_score", 0),
                article.get("heuristic_score", 0),
                article.get("age_hours", 0),
                article.get("published_iso", ""),
                now,
            ))
            return cur.lastrowid or 0

    def save_articles(self, articles: list[dict]) -> int:
        """Bulk save articles. Returns count saved."""
        return sum(1 for a in articles if self.save_article(a))

    def get_top_article(self) -> Optional[dict]:
        """Get the highest-scored unused article."""
        with _get_conn(self.db_path) as conn:
            row = conn.execute("""
                SELECT * FROM articles
                WHERE status = 'pending'
                ORDER BY claude_score DESC, heuristic_score DESC
                LIMIT 1
            """).fetchone()
            return dict(row) if row else None

    def get_recent_articles(self, limit: int = 50, status: Optional[str] = None) -> list[dict]:
        """Get recent articles, optionally filtered by status."""
        with _get_conn(self.db_path) as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM articles WHERE status=? ORDER BY fetched_at DESC LIMIT ?",
                    (status, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM articles ORDER BY fetched_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

    def mark_article_used(self, article_id: int) -> None:
        with _get_conn(self.db_path) as conn:
            conn.execute("UPDATE articles SET status='used' WHERE id=?", (article_id,))

    # ── Article deduplication (persistent across runs) ────────────────────

    def has_article_hash(self, hash_hex: str) -> bool:
        """Check if an article title hash already exists in the DB."""
        with _get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM article_hashes WHERE hash=?", (hash_hex,)
            ).fetchone()
            return row is not None

    def get_seen_hashes(self) -> set:
        """Load all known article hashes into a set for batch dedup."""
        with _get_conn(self.db_path) as conn:
            rows = conn.execute("SELECT hash FROM article_hashes").fetchall()
            return {r["hash"] for r in rows}

    def store_article_hashes(self, articles: list[dict]) -> int:
        """
        Store title hashes for a batch of articles.
        Returns the number of new hashes stored.
        """
        import hashlib
        now = datetime.now(timezone.utc).isoformat()
        stored = 0
        with _get_conn(self.db_path) as conn:
            for a in articles:
                h = hashlib.md5(a["title"].lower().strip().encode()).hexdigest()
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO article_hashes (hash, title, source, first_seen) VALUES (?,?,?,?)",
                        (h, a["title"], a.get("source", ""), now),
                    )
                    stored += 1
                except Exception:
                    pass
        return stored

    def prune_old_hashes(self, max_age_days: int = 30) -> int:
        """Remove hashes older than max_age_days to prevent unbounded growth."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        with _get_conn(self.db_path) as conn:
            cur = conn.execute("DELETE FROM article_hashes WHERE first_seen < ?", (cutoff,))
            deleted = cur.rowcount
            if deleted:
                logger.info(f"Pruned {deleted} old article hashes (>{max_age_days}d)")
            return deleted

    # ── Scripts ──────────────────────────────────────────────────────────

    def save_script(self, package: dict) -> None:
        """Save a generated script package."""
        quality = package.get("quality", {})
        with _get_conn(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO scripts
                  (content_id, pillar, script, word_count, overall_score,
                   hook_strength, simplicity, bolt_voice, pacing,
                   quality_json, captions_json, status, auto_approved, generated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                package["content_id"],
                package.get("pillar", "ai_news"),
                package.get("script", ""),
                quality.get("word_count", 0),
                quality.get("overall_score", 0),
                quality.get("hook_strength", 0),
                quality.get("simplicity", 0),
                quality.get("bolt_voice", 0),
                quality.get("pacing", 0),
                json.dumps(quality),
                json.dumps(package.get("captions", {})),
                package.get("status", "pending_review"),
                1 if package.get("auto_approved") else 0,
                package.get("generated_at", datetime.now(timezone.utc).isoformat()),
            ))

    def get_pending_script(self) -> Optional[dict]:
        """Get the next approved script waiting for video production."""
        with _get_conn(self.db_path) as conn:
            row = conn.execute("""
                SELECT s.*, v.status as video_status
                FROM scripts s
                LEFT JOIN videos v ON s.content_id = v.content_id
                WHERE s.status IN ('approved', 'pending_review')
                  AND (v.content_id IS NULL OR v.status = 'pending')
                ORDER BY s.overall_score DESC
                LIMIT 1
            """).fetchone()
            if not row:
                return None
            d = dict(row)
            d["quality"] = json.loads(d.pop("quality_json", "{}"))
            d["captions"] = json.loads(d.pop("captions_json", "{}"))
            return d

    def approve_script(self, content_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with _get_conn(self.db_path) as conn:
            conn.execute("""
                UPDATE scripts SET status='approved', approved_at=? WHERE content_id=?
            """, (now, content_id))

    def reject_script(self, content_id: str, reason: str = "") -> None:
        with _get_conn(self.db_path) as conn:
            conn.execute("""
                UPDATE scripts SET status='rejected', review_decision=? WHERE content_id=?
            """, (reason, content_id))

    def get_scripts(self, status: Optional[str] = None, limit: int = 50) -> list[dict]:
        """Get scripts, optionally filtered by status."""
        with _get_conn(self.db_path) as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM scripts WHERE status=? ORDER BY generated_at DESC LIMIT ?",
                    (status, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM scripts ORDER BY generated_at DESC LIMIT ?", (limit,)
                ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["quality"] = json.loads(d.pop("quality_json", "{}"))
                d["captions"] = json.loads(d.pop("captions_json", "{}"))
                result.append(d)
            return result

    # ── Videos ───────────────────────────────────────────────────────────

    def save_video(self, package: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with _get_conn(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO videos
                  (content_id, audio_path, audio_provider, avatar_path,
                   avatar_provider, final_path, thumbnail_path, video_ready,
                   status, completed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                package["content_id"],
                package.get("audio_path"),
                package.get("audio_provider", "edge_tts"),
                package.get("avatar_video_path"),
                package.get("avatar_provider"),
                package.get("final_video_path"),
                package.get("thumbnail_path"),
                1 if package.get("video_ready") else 0,
                package.get("status", "pending"),
                package.get("video_completed_at", now),
            ))

    def update_video_status(self, content_id: str, status: str, **fields) -> None:
        """
        Incremental video status update -- write partial progress to DB
        so crash recovery can resume from the last successful sub-step.

        Pre-plan pattern: after audio -> status='audio_ready', after avatar
        -> status='avatar_ready', after assembly -> status='assembled'.
        """
        now = datetime.now(timezone.utc).isoformat()
        # Build dynamic SET clause from provided fields
        set_parts = ["status=?", "completed_at=?"]
        params: list = [status, now]
        for col, val in fields.items():
            set_parts.append(f"{col}=?")
            params.append(val)
        params.append(content_id)
        sql = f"UPDATE videos SET {', '.join(set_parts)} WHERE content_id=?"
        with _get_conn(self.db_path) as conn:
            conn.execute(sql, params)

    def ensure_video_row(self, content_id: str) -> None:
        """Create a pending video row if one doesn't exist yet."""
        with _get_conn(self.db_path) as conn:
            conn.execute("""
                INSERT OR IGNORE INTO videos (content_id, status, completed_at)
                VALUES (?, 'pending', ?)
            """, (content_id, datetime.now(timezone.utc).isoformat()))

    def get_video_status(self, content_id: str) -> Optional[dict]:
        """Get the current video record for resume-after-crash logic."""
        with _get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM videos WHERE content_id=?", (content_id,)
            ).fetchone()
            return dict(row) if row else None

    # ── Publications ─────────────────────────────────────────────────────

    def save_publication(self, content_id: str, platform: str,
                          success: bool, url: str = "", post_id: str = "",
                          error_msg: str = "", scheduled_at: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with _get_conn(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO publications
                  (content_id, platform, success, post_url, post_id,
                   error_msg, scheduled_at, published_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (content_id, platform, 1 if success else 0,
                  url, post_id, error_msg, scheduled_at, now))

    def update_publication_metrics(self, content_id: str, platform: str,
                                    views: int, engagement_rate: float) -> None:
        """Update views and engagement_rate on a publication (24h after posting)."""
        with _get_conn(self.db_path) as conn:
            conn.execute("""
                UPDATE publications
                SET views=?, engagement_rate=?
                WHERE content_id=? AND platform=?
            """, (views, engagement_rate, content_id, platform))

    def get_publications_needing_metrics(self, min_age_hours: int = 24) -> list:
        """Return publications older than min_age_hours that have no metrics yet.

        Pre-plan: '24 hours after posting, the analytics tracker fetches views,
        retention rate, likes, and comments from each platform. These update the
        Publication records.'
        """
        with _get_conn(self.db_path) as conn:
            rows = conn.execute("""
                SELECT content_id, platform, post_url, post_id, published_at
                FROM publications
                WHERE success = 1
                  AND (views IS NULL OR views = 0)
                  AND published_at IS NOT NULL
                  AND julianday('now') - julianday(published_at) >= ?
            """, (min_age_hours / 24.0,)).fetchall()
        return [dict(r) for r in rows]

    def save_publish_results(self, content_id: str, results: dict) -> None:
        """Save publish results for all platforms from a results dict."""
        for platform, r in results.items():
            self.save_publication(
                content_id=content_id,
                platform=platform,
                success=r.get("success", False),
                url=r.get("url", ""),
                post_id=r.get("video_id", r.get("media_id", r.get("buffer_id", ""))),
                error_msg=r.get("error", ""),
            )

    # ── Analytics ─────────────────────────────────────────────────────────

    def save_analytics_snapshot(self, platform: str, data: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with _get_conn(self.db_path) as conn:
            conn.execute("""
                INSERT INTO analytics_snapshots
                  (fetched_at, platform, followers, views_30d, engagement_rate, video_count, raw_json)
                VALUES (?,?,?,?,?,?,?)
            """, (
                now, platform,
                data.get("followers", data.get("subscribers", 0)),
                data.get("recent_30_views", data.get("recent_20_views", 0)),
                data.get("engagement_rate", 0),
                data.get("video_count", 0),
                json.dumps(data),
            ))

    def get_latest_analytics(self) -> dict:
        """Get most recent snapshot per platform."""
        with _get_conn(self.db_path) as conn:
            rows = conn.execute("""
                SELECT a1.* FROM analytics_snapshots a1
                INNER JOIN (
                    SELECT platform, MAX(fetched_at) AS max_dt
                    FROM analytics_snapshots GROUP BY platform
                ) a2 ON a1.platform = a2.platform AND a1.fetched_at = a2.max_dt
            """).fetchall()
            return {row["platform"]: json.loads(row["raw_json"]) for row in rows}

    # ── Cost tracking ─────────────────────────────────────────────────────

    def record_cost(self, service: str, operation: str, quantity: float,
                     cost_usd: float, model: str = "", content_id: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with _get_conn(self.db_path) as conn:
            conn.execute("""
                INSERT INTO cost_events
                  (recorded_at, service, operation, quantity, cost_usd, model, content_id)
                VALUES (?,?,?,?,?,?,?)
            """, (now, service, operation, quantity, cost_usd, model, content_id))

    def get_cost_summary(self, month: Optional[str] = None) -> dict:
        """Get cost summary, optionally for a specific month (YYYY-MM)."""
        with _get_conn(self.db_path) as conn:
            if month:
                rows = conn.execute("""
                    SELECT service, SUM(cost_usd) as total, COUNT(*) as calls
                    FROM cost_events
                    WHERE recorded_at LIKE ?
                    GROUP BY service ORDER BY total DESC
                """, (f"{month}%",)).fetchall()
                total = conn.execute(
                    "SELECT SUM(cost_usd) FROM cost_events WHERE recorded_at LIKE ?", (f"{month}%",)
                ).fetchone()[0] or 0
            else:
                rows = conn.execute("""
                    SELECT service, SUM(cost_usd) as total, COUNT(*) as calls
                    FROM cost_events GROUP BY service ORDER BY total DESC
                """).fetchall()
                total = conn.execute("SELECT SUM(cost_usd) FROM cost_events").fetchone()[0] or 0

            return {
                "total_usd": round(total, 6),
                "by_service": {r["service"]: {"total": round(r["total"], 6), "calls": r["calls"]} for r in rows},
                "month": month,
            }

    # ── Job queue ─────────────────────────────────────────────────────────

    def enqueue_job(self, job_type: str, content_id: str = "",
                     max_attempts: int = 3) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with _get_conn(self.db_path) as conn:
            cur = conn.execute("""
                INSERT INTO jobs (job_type, content_id, status, max_attempts, created_at, updated_at)
                VALUES (?,?,?,?,?,?)
            """, (job_type, content_id, "pending", max_attempts, now, now))
            return cur.lastrowid

    def get_script_by_content_id(self, content_id: str) -> Optional[dict]:
        """Get a single script by its content_id."""
        with _get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM scripts WHERE content_id=?", (content_id,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["quality"] = json.loads(d.pop("quality_json", "{}"))
            d["captions"] = json.loads(d.pop("captions_json", "{}"))
            return d

    def get_pending_jobs(self, job_type: Optional[str] = None, limit: int = 10) -> list[dict]:
        """Get jobs ready to run (pending or retrying with next_run_at in the past)."""
        now = datetime.now(timezone.utc).isoformat()
        with _get_conn(self.db_path) as conn:
            if job_type:
                rows = conn.execute("""
                    SELECT * FROM jobs
                    WHERE job_type=? AND status IN ('pending','retrying')
                      AND (next_run_at IS NULL OR next_run_at <= ?)
                      AND attempts < max_attempts
                    ORDER BY created_at ASC LIMIT ?
                """, (job_type, now, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM jobs
                    WHERE status IN ('pending','retrying')
                      AND (next_run_at IS NULL OR next_run_at <= ?)
                      AND attempts < max_attempts
                    ORDER BY created_at ASC LIMIT ?
                """, (now, limit)).fetchall()
            return [dict(r) for r in rows]

    def fail_job(self, job_id: int, error: str, retry_after_seconds: int = 300) -> None:
        """Mark a job as failed and schedule a retry."""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        next_run = (now + timedelta(seconds=retry_after_seconds)).isoformat()
        with _get_conn(self.db_path) as conn:
            conn.execute("""
                UPDATE jobs SET status='retrying', attempts=attempts+1,
                  error_msg=?, next_run_at=?, updated_at=?
                WHERE id=?
            """, (error[:500], next_run, now.isoformat(), job_id))

    def save_dead_letter(self, job: dict) -> None:
        """Record a permanently failed job in the dead_letters table."""
        now = datetime.now(timezone.utc).isoformat()
        with _get_conn(self.db_path) as conn:
            conn.execute("""
                INSERT INTO dead_letters (job_id, job_type, content_id, error_msg, attempts, dead_at)
                VALUES (?,?,?,?,?,?)
            """, (
                job.get("id"),
                job.get("job_type", "unknown"),
                job.get("content_id", ""),
                job.get("error_msg", "max attempts exceeded"),
                job.get("attempts", 0),
                now,
            ))

    def complete_job(self, job_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with _get_conn(self.db_path) as conn:
            conn.execute("UPDATE jobs SET status='done', updated_at=? WHERE id=?", (now, job_id))

    # ── Dashboard stats ───────────────────────────────────────────────────

    def get_dashboard_summary(self) -> dict:
        """Aggregate stats for the dashboard overview."""
        with _get_conn(self.db_path) as conn:
            total_published = conn.execute(
                "SELECT COUNT(DISTINCT content_id) FROM publications WHERE success=1"
            ).fetchone()[0] or 0
            pending_review = conn.execute(
                "SELECT COUNT(*) FROM scripts WHERE status='pending_review'"
            ).fetchone()[0] or 0
            total_cost = conn.execute(
                "SELECT SUM(cost_usd) FROM cost_events"
            ).fetchone()[0] or 0
            failed_jobs = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status='retrying' AND attempts >= max_attempts"
            ).fetchone()[0] or 0
            platform_breakdown = conn.execute("""
                SELECT platform, COUNT(*) as count
                FROM publications WHERE success=1
                GROUP BY platform
            """).fetchall()

            return {
                "total_published": total_published,
                "pending_review": pending_review,
                "total_cost_usd": round(total_cost, 4),
                "failed_jobs": failed_jobs,
                "by_platform": {r["platform"]: r["count"] for r in platform_breakdown},
            }


# ── Module-level singleton ─────────────────────────────────────────────────

_db_instance: Optional[BoltDB] = None


def get_db(db_path: Optional[str] = None) -> BoltDB:
    """Get or create the database singleton."""
    global _db_instance
    if _db_instance is None:
        path = Path(db_path) if db_path else DEFAULT_DB_PATH
        _db_instance = BoltDB(path)
    return _db_instance


# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Bolt AI — Database CLI")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("stats", help="Show database statistics")
    sub.add_parser("costs", help="Show cost breakdown")
    sq = sub.add_parser("scripts", help="List scripts")
    sq.add_argument("--status", choices=["pending_review","approved","published","rejected"], help="Filter by status")
    sub.add_parser("jobs", help="Show pending retry jobs")
    args = parser.parse_args()

    db = get_db()

    if args.cmd == "stats":
        summary = db.get_dashboard_summary()
        print(f"\n{'─'*45}\n  📊 Bolt Database Stats\n{'─'*45}")
        print(f"  Published videos:  {summary['total_published']}")
        print(f"  Pending review:    {summary['pending_review']}")
        print(f"  Total API cost:    ${summary['total_cost_usd']:.4f}")
        print(f"  Failed jobs:       {summary['failed_jobs']}")
        for p, c in summary['by_platform'].items():
            print(f"  {p:15s}:  {c} videos")
        print(f"{'─'*45}\n")

    elif args.cmd == "costs":
        m = datetime.now().strftime("%Y-%m")
        summary = db.get_cost_summary(month=m)
        print(f"\nCosts for {m}: ${summary['total_usd']:.4f}")
        for svc, data in summary["by_service"].items():
            print(f"  {svc:20s} ${data['total']:.6f} ({data['calls']} calls)")

    elif args.cmd == "scripts":
        scripts = db.get_scripts(status=getattr(args, "status", None), limit=20)
        for s in scripts:
            print(f"  [{s['status']:14s}] [{s['overall_score']:4.1f}] {s['content_id']} — {s['script'][:50]}...")

    elif args.cmd == "jobs":
        jobs = db.get_pending_jobs()
        if not jobs:
            print("No pending retry jobs.")
        for j in jobs:
            print(f"  [{j['status']:10s}] {j['job_type']:10s} attempt {j['attempts']}/{j['max_attempts']} — {j['error_msg'][:60]}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

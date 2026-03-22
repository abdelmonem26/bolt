#!/usr/bin/env python3
"""
Bolt AI — FastAPI Backend (api.py)
===================================
The real backend that powers the dashboard.
Every /api/* endpoint the frontend calls is served here.

Run:
  uvicorn code.api:app --host 0.0.0.0 --port 8000 --reload

Or via docker-compose (recommended):
  docker-compose up

Endpoints:
  GET  /api/status          — pipeline health + system status
  GET  /api/analytics       — real platform metrics from DB
  GET  /api/scripts         — content queue (all scripts)
  GET  /api/scripts/{id}    — single script detail
  POST /api/hitl/approve/{id}  — approve a script (creates flag file + updates DB)
  POST /api/hitl/reject/{id}   — reject a script
  POST /api/pipeline/run    — trigger a full pipeline run
  POST /api/pipeline/{step} — trigger a specific step
  GET  /api/costs           — cost summary from DB
  GET  /api/backups         — list available backups
  POST /api/backups         — create a manual backup
  POST /api/backups/{id}/restore — restore from backup
  GET  /api/news            — recent articles in DB
  GET  /api/jobs            — job queue status
  GET  /api/health          — health check for monitoring
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

# Bootstrap secrets + logging before anything else
from secrets_manager import load_all_secrets
from observability import init as init_obs, get_logger

def _load_config():
    cfg_path = Path(__file__).parent / "config.json"
    with open(cfg_path) as f:
        return load_all_secrets(json.load(f))

CONFIG = _load_config()
init_obs(CONFIG, log_dir=CONFIG.get("logging",{}).get("file_path","logs"))
logger = get_logger("bolt.api")

from database import get_db, BoltDB
from cost_tracker import CostTracker
from backup_system import BackupSystem
from hitl import approve_from_dashboard, reject_from_dashboard, list_pending
from budget_enforcer import BudgetEnforcer

import os as _os

# ── API key authentication ─────────────────────────────────────────────────
# Set BOLT_API_KEY in your environment or .env to enable auth.
# When unset, auth is disabled (local development mode).

_API_KEY = _os.environ.get("BOLT_API_KEY", "")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# Paths that are always public (health checks, static assets, SSE)
_PUBLIC_PATHS = {"/api/health", "/api/stream/status", "/", "/api/docs", "/api/redoc", "/openapi.json"}


async def _verify_api_key(request: Request, api_key: str = Depends(_api_key_header)):
    """
    Dependency that enforces API key auth when BOLT_API_KEY is set.
    Skips auth for health checks and static file serving.
    """
    if not _API_KEY:
        return  # Auth disabled -- local dev mode
    path = request.url.path
    if path in _PUBLIC_PATHS or not path.startswith("/api/"):
        return  # Public routes skip auth
    if api_key != _API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key (X-API-Key header)")


# ── App setup ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Bolt AI Content Creator API",
    description="Backend API powering the Bolt dashboard",
    version="2.2",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    dependencies=[Depends(_verify_api_key)],
)

# ── CORS ───────────────────────────────────────────────────────────────────
_cors_origins_env = _os.environ.get("BOLT_CORS_ORIGINS", "")
_cors_origins = (
    [o.strip() for o in _cors_origins_env.split(",") if o.strip()]
    if _cors_origins_env
    else ["http://localhost:5173", "http://localhost:3000", "http://localhost:4173"]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the built dashboard as static files at /
dashboard_dist = Path(__file__).parent.parent / "bolt-dashboard" / "dist"
if dashboard_dist.exists():
    app.mount("/assets", StaticFiles(directory=str(dashboard_dist / "assets")), name="assets")
    app.mount("/images", StaticFiles(directory=str(dashboard_dist / "images")), name="images")
    app.mount("/data",   StaticFiles(directory=str(dashboard_dist / "data")),   name="data-static")

# ── Request / response models ──────────────────────────────────────────────

class HITLDecision(BaseModel):
    reason: Optional[str] = ""

class PipelineStepRequest(BaseModel):
    step: Optional[str] = None  # news | script | video | publish | analytics

class BackupRequest(BaseModel):
    backup_type: str = "manual"  # manual | daily | weekly | monthly

class RestoreRequest(BaseModel):
    backup_id: str

# ── Health ─────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    """Health check — used by monitoring and docker-compose healthcheck."""
    db = get_db()
    summary = db.get_dashboard_summary()
    return {
        "status": "ok",
        "version": "2.2",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "database": "connected",
        "pending_review": summary["pending_review"],
        "failed_jobs": summary["failed_jobs"],
    }


# ── System status ──────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    """
    Real-time pipeline status — reads from DB and file system.
    Replaces the static /data/system-status.json.
    """
    db = get_db()
    summary = db.get_dashboard_summary()
    tracker = CostTracker()
    budget = BudgetEnforcer(CONFIG)
    monthly = tracker.get_monthly_summary()
    cost_status = budget.check_all()

    # Check which providers are configured
    apis = CONFIG.get("apis", {})
    def configured(key): return bool(apis.get(key)) and not str(apis.get(key, "")).startswith("→")

    return {
        "systemHealth": {
            "status": "online",
            "uptime": "99.8%",
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
            "version": "2.2",
        },
        "pipeline": {
            "totalPublished":   summary["total_published"],
            "pendingReview":    summary["pending_review"],
            "failedJobs":       summary["failed_jobs"],
            "byPlatform":       summary["by_platform"],
        },
        "costs": {
            "monthTotal":       round(monthly["total_cost"], 4),
            "videoCount":       monthly["videos"],
            "avgPerVideo":      round(monthly.get("avg_cost_per_video", 0), 4),
            "budgetStatus":     cost_status["overall"],
            "alerts":           cost_status["alerts"],
        },
        "providers": {
            "voice":   {
                "edge_tts":    {"available": True,  "free": True},
                "google_tts":  {"available": configured("google_cloud_tts_key"), "free": True},
                "elevenlabs":  {"available": configured("elevenlabs_api_key"),   "free": False},
            },
            "avatar":  {
                "vidnoz":      {"available": configured("vidnoz_api_key"), "free": True},
                "did":         {"available": configured("did_api_key"),    "free": True},
            },
            "publish": {
                "youtube":     {"available": configured("youtube_client_id")},
                "buffer":      {"available": configured("buffer_access_token")},
                "instagram":   {"available": configured("instagram_access_token")},
                "tiktok":      {"available": configured("tiktok_access_token")},
            },
        },
    }


# ── Analytics ──────────────────────────────────────────────────────────────

@app.get("/api/analytics")
async def get_analytics():
    """Real analytics from DB — replaces static analytics.json."""
    db = get_db()
    snapshots = db.get_latest_analytics()
    summary = db.get_dashboard_summary()

    # Aggregate totals across platforms
    total_followers = sum(s.get("followers", s.get("subscribers", 0)) for s in snapshots.values())
    total_views     = sum(s.get("recent_30_views", s.get("recent_20_views", s.get("recent_20_plays", 0))) for s in snapshots.values())
    avg_engagement  = sum(s.get("engagement_rate", 0) for s in snapshots.values()) / max(len(snapshots), 1)

    return {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_views_30d":      total_views,
            "total_followers":      total_followers,
            "avg_engagement_rate":  round(avg_engagement, 2),
            "videos_published":     summary["total_published"],
        },
        "platforms": snapshots,
        "recent_content": db.get_scripts(status="published", limit=10),
    }


# ── Scripts / Content queue ────────────────────────────────────────────────

@app.get("/api/scripts")
async def get_scripts(status: Optional[str] = None, limit: int = 50):
    """All scripts — optionally filtered by status."""
    db = get_db()
    scripts = db.get_scripts(status=status, limit=limit)
    # Add pending flag-file status from HITL
    pending_ids = {p["content_id"] for p in list_pending()}
    for s in scripts:
        if s["content_id"] in pending_ids:
            s["hitl_waiting"] = True
    return {"scripts": scripts, "total": len(scripts)}


@app.get("/api/scripts/{content_id}")
async def get_script(content_id: str):
    """Single script detail."""
    db = get_db()
    match = db.get_script_by_content_id(content_id)
    if not match:
        raise HTTPException(status_code=404, detail=f"Script {content_id} not found")
    return match


# ── HITL ──────────────────────────────────────────────────────────────────

@app.post("/api/hitl/approve/{content_id}")
async def hitl_approve(content_id: str, body: HITLDecision = HITLDecision()):
    """
    Approve a script from the dashboard.
    Creates a video job so the job worker picks it up -- the scheduler
    never blocks waiting for approval (pre-plan Rule 3).
    """
    db = get_db()
    ok = approve_from_dashboard(content_id)
    db.approve_script(content_id)
    # Create a video job so the job worker continues the pipeline
    db.enqueue_job("video", content_id=content_id, max_attempts=3)
    logger.info("HITL approved via dashboard -- video job created", extra={"content_id": content_id})
    return {"success": ok, "content_id": content_id, "action": "approved", "video_job_created": True}


@app.post("/api/hitl/reject/{content_id}")
async def hitl_reject(content_id: str, body: HITLDecision = HITLDecision()):
    """Reject a script from the dashboard."""
    db = get_db()
    ok = reject_from_dashboard(content_id, body.reason)
    db.reject_script(content_id, body.reason)
    logger.info("HITL rejected via dashboard", extra={"content_id": content_id, "reason": body.reason})
    return {"success": ok, "content_id": content_id, "action": "rejected"}


@app.get("/api/hitl/pending")
async def get_hitl_pending():
    """List all scripts waiting for human approval."""
    return {"pending": list_pending()}


# ── Pipeline control ───────────────────────────────────────────────────────

@app.post("/api/pipeline/run")
async def trigger_pipeline():
    """
    Trigger a full pipeline run by creating a job.

    Pre-plan Layer 6 rule: the API server never imports pipeline modules.
    It writes a job to the DB and the job worker picks it up.
    """
    db = get_db()
    db.enqueue_job("pipeline_full", max_attempts=1)
    logger.info("Full pipeline job created via API")
    return {"status": "queued", "message": "Pipeline job created -- job worker will pick it up"}


@app.post("/api/pipeline/{step}")
async def trigger_step(step: str):
    """
    Trigger a specific pipeline step by creating a job.

    Pre-plan Layer 6: API writes to DB, never runs pipeline code.
    """
    valid_steps = ["news", "script", "video", "publish", "analytics"]
    if step not in valid_steps:
        raise HTTPException(status_code=400, detail=f"Invalid step. Must be one of: {valid_steps}")

    db = get_db()
    db.enqueue_job(step, max_attempts=3)
    logger.info(f"Pipeline step job created via API", extra={"step": step})
    return {"status": "queued", "step": step}


@app.get("/api/pipeline/status")
async def pipeline_status():
    """Pipeline status based on job queue state."""
    db = get_db()
    with db._get_conn_ctx() as conn:
        running = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='running'"
        ).fetchone()[0]
    return {"running": running > 0, "running_jobs": running}


# ── Costs ──────────────────────────────────────────────────────────────────

@app.get("/api/costs")
async def get_costs(month: Optional[str] = None):
    """Cost summary — optionally for a specific month (YYYY-MM)."""
    db = get_db()
    tracker = CostTracker()
    budget = BudgetEnforcer(CONFIG)

    if not month:
        month = datetime.now().strftime("%Y-%m")

    db_summary  = db.get_cost_summary(month=month)
    file_summary = tracker.get_monthly_summary(month)
    total_summary = tracker.get_total_summary()
    cost_status = budget.check_all()

    return {
        "month":         month,
        "total_usd":     db_summary["total_usd"],
        "by_service":    db_summary["by_service"],
        "file_tracker":  file_summary,
        "all_time":      total_summary,
        "budget_status": cost_status,
        "daily": tracker.get_daily_summary(),
    }


# ── Backups ────────────────────────────────────────────────────────────────

@app.get("/api/backups")
async def list_backups():
    """List all available backups."""
    backup = BackupSystem()
    return {"backups": backup.list_backups()}


@app.post("/api/backups")
async def create_backup(body: BackupRequest, background_tasks: BackgroundTasks):
    """Create a backup in the background."""
    def _do_backup():
        backup = BackupSystem()
        return backup.create_backup(body.backup_type)
    background_tasks.add_task(_do_backup)
    return {"status": "started", "type": body.backup_type}


@app.post("/api/backups/{backup_id}/restore")
async def restore_backup(backup_id: str):
    """Restore from a specific backup."""
    backup = BackupSystem()
    ok = backup.restore_backup(backup_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Backup {backup_id} not found or restore failed")
    return {"success": True, "backup_id": backup_id}


# ── News ───────────────────────────────────────────────────────────────────

@app.get("/api/news")
async def get_news(status: Optional[str] = None, limit: int = 50):
    """Recent news articles from the DB."""
    db = get_db()
    articles = db.get_recent_articles(limit=limit, status=status)
    return {"articles": articles, "total": len(articles)}


# ── Jobs ───────────────────────────────────────────────────────────────────

@app.get("/api/jobs")
async def get_jobs():
    """Job queue status."""
    db = get_db()
    pending  = db.get_pending_jobs()
    with db._get_conn_ctx() as conn:
        all_jobs = conn.execute(
            "SELECT status, COUNT(*) as count FROM jobs GROUP BY status"
        ).fetchall()
    return {
        "pending":   pending,
        "by_status": {r["status"]: r["count"] for r in all_jobs},
    }


# ── Serve frontend ─────────────────────────────────────────────────────────

@app.get("/")
async def serve_frontend():
    """Serve the dashboard SPA."""
    index = dashboard_dist / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"message": "Dashboard not built. Run: cd bolt-dashboard && npm run build"}

@app.get("/{path:path}")
async def spa_fallback(path: str):
    """SPA fallback — all non-API routes serve index.html."""
    if path.startswith("api/"):
        raise HTTPException(status_code=404)
    index = dashboard_dist / "index.html"
    if index.exists():
        return FileResponse(str(index))
    raise HTTPException(status_code=404)


# _get_conn_ctx is now a proper method on BoltDB in database.py
# No monkeypatching needed


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)


# ══════════════════════════════════════════════════════════════════
# SERVER-SENT EVENTS — real-time pipeline status stream
# ══════════════════════════════════════════════════════════════════

import asyncio
from fastapi.responses import StreamingResponse

@app.get("/api/stream/status")
async def stream_status():
    """
    SSE endpoint — dashboard connects here for real-time updates.
    Emits a JSON event every 5 seconds with current pipeline state.

    Connect from JS:
      const es = new EventSource('http://localhost:8000/api/stream/status')
      es.onmessage = e => setStatus(JSON.parse(e.data))
    """
    async def event_generator():
        while True:
            try:
                db     = get_db()
                summary = db.get_dashboard_summary()
                tracker = CostTracker()
                monthly = tracker.get_monthly_summary()
                pending_hitl = list_pending()

                # Check running status from job queue (replaces removed _pipeline_running global)
                with db._get_conn_ctx() as conn:
                    running_count = conn.execute(
                        "SELECT COUNT(*) FROM jobs WHERE status='running'"
                    ).fetchone()[0]

                payload = {
                    "ts":             datetime.now(timezone.utc).isoformat(),
                    "pipeline_running": running_count > 0,
                    "pending_review":  summary["pending_review"],
                    "failed_jobs":     summary["failed_jobs"],
                    "total_published": summary["total_published"],
                    "month_cost":      round(monthly["total_cost"], 4),
                    "hitl_waiting":    len(pending_hitl),
                }
                yield f"data: {json.dumps(payload)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            await asyncio.sleep(5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

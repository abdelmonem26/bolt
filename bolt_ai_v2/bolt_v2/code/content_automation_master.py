#!/usr/bin/env python3
"""
Bolt AI — Master Orchestrator v2.2
====================================
Integrates: free tools + notifications + cost tracking + backups +
            secrets management + SQLite DB + structured logging + rate limiter + HITL

Usage:
  python content_automation_master.py                     # Full pipeline
  python content_automation_master.py --step news
  python content_automation_master.py --step script
  python content_automation_master.py --step video
  python content_automation_master.py --step publish
  python content_automation_master.py --step analytics
  python content_automation_master.py --backup manual
  python content_automation_master.py --cost-summary
  python content_automation_master.py --list-backups
  python content_automation_master.py --db-stats
  python content_automation_master.py --secrets-audit
  python content_automation_master.py --schedule          # 24/7 daemon
"""

import argparse, asyncio, json, sys, time
from datetime import datetime, timezone
from pathlib import Path

# ── 1. Bootstrap: secrets + structured logging (must happen first) ─────────
sys.path.insert(0, str(Path(__file__).parent))

# Load secrets from .env before anything else touches config
from secrets_manager import load_all_secrets, print_audit as secrets_audit

def load_config(path="code/config.json"):
    with open(path) as f:
        raw = json.load(f)
    return load_all_secrets(raw)   # Injects .env secrets, removes YOUR_* placeholders

CONFIG = load_config()

# ── 2. Structured logging ──────────────────────────────────────────────────
from observability import init as init_observability, get_logger, get_rate_limiter
log_cfg = CONFIG.get("logging", {})
init_observability(CONFIG, log_dir=log_cfg.get("file_path","logs"), log_level=log_cfg.get("level","INFO"))
logger = get_logger("bolt.master")
rate_limiter = get_rate_limiter(CONFIG)

# ── 3. Import pipeline + enhanced modules ─────────────────────────────────
try:
    import news_aggregator, script_generator, video_pipeline
    import platform_publisher, analytics_tracker
    from notifications import NotificationManager, Notification, NotificationLevel
    from cost_tracker import CostTracker
    from backup_system import BackupSystem
    from database import get_db
    from hitl import wait_for_approval
    from budget_enforcer import BudgetEnforcer, BudgetExceededError
    logger.info("All modules loaded", extra={"version": "2.2"})
except ImportError as e:
    print(f"Import error: {e}\nRun: pip install -r requirements.txt")
    sys.exit(1)

# ── Helpers ────────────────────────────────────────────────────────────────

def get_notifier(): return NotificationManager(CONFIG)

def notify(notifier, title, message, level="info", metadata=None):
    lmap = {"debug":NotificationLevel.DEBUG,"info":NotificationLevel.INFO,
            "warning":NotificationLevel.WARNING,"error":NotificationLevel.ERROR}
    notifier.send(Notification(title=title,message=message,
                               level=lmap.get(level,NotificationLevel.INFO),
                               metadata=metadata or {}))

class CircuitBreaker:
    def __init__(self, threshold=5, timeout_minutes=15):
        self.threshold=threshold; self.timeout=timeout_minutes*60
        self.failures={}; self.open_until={}
    def is_open(self, svc):
        if svc in self.open_until:
            if time.time() < self.open_until[svc]: return True
            del self.open_until[svc]; self.failures[svc]=0
        return False
    def record_failure(self, svc):
        self.failures[svc]=self.failures.get(svc,0)+1
        if self.failures[svc]>=self.threshold:
            self.open_until[svc]=time.time()+self.timeout
            logger.warning(f"Circuit OPEN: {svc}", extra={"service":svc,"timeout_min":self.timeout//60})
    def record_success(self, svc): self.failures[svc]=0

# ── Pipeline steps ─────────────────────────────────────────────────────────

# ── Error categories (pre-plan Section 16) ─────────────────────────────
# Transient: retry (ConnectionError, TimeoutError, etc.)
# Configuration: notify, no retry (missing keys, expired tokens)
# Programmer: let it crash (KeyError, TypeError, AttributeError)

_TRANSIENT_ERRORS = (ConnectionError, TimeoutError, OSError)


async def step_news(notifier, cb, tracker):
    logger.info("STEP 1 — NEWS AGGREGATION")
    if cb.is_open("news"): return False
    db = get_db()
    try:
        await rate_limiter.acquire("claude")
        articles = await news_aggregator.run(CONFIG)
    except _TRANSIENT_ERRORS as e:
        cb.record_failure("news")
        logger.warning("News step transient failure", extra={"error": str(e)})
        db.enqueue_job("news", max_attempts=3)
        return False
    # Programmer errors (KeyError, TypeError) intentionally NOT caught -- let them crash.

    if articles:
        try:
            from content_extractor import fuzzy_classify
            for a in articles:
                extraction = fuzzy_classify(a.get("title", ""), a.get("summary", ""))
                a["category"] = extraction.category
                a["sentiment"] = extraction.sentiment
                a["impact_score"] = extraction.impact_score
                a["companies_mentioned"] = extraction.companies_mentioned
                a["technologies_mentioned"] = extraction.technologies_mentioned
        except ImportError:
            logger.debug("content_extractor not available -- skipping enrichment")
        db.save_articles(articles)
        cb.record_success("news")
        notify(notifier, "News Aggregated",
               f"Top: {articles[0]['title'][:55]}\nScore: {articles[0].get('claude_score',0):.1f}/10",
               "info", {"count": len(articles)})
        logger.info("News step done", extra={"articles_queued": len(articles)})
    return True


def step_script(notifier, cb, tracker):
    logger.info("STEP 2 — SCRIPT GENERATION")
    db = get_db()
    try:
        result = script_generator.run(CONFIG)
    except _TRANSIENT_ERRORS as e:
        cb.record_failure("claude")
        logger.warning("Script step transient failure", extra={"error": str(e)})
        return None

    if result:
        score = result["quality"]["overall_score"]
        qg = CONFIG.get("quality_gate", {})
        if score < qg.get("auto_reject_below", 6.0):
            notify(notifier, "Script Auto-Rejected", f"Score {score:.1f} below threshold", "warning")
            logger.warning("Script auto-rejected", extra={"score": score, "content_id": result["content_id"]})
            return None
        tracker.record_usage("claude", "input", 0.4, "claude-3-sonnet")
        tracker.record_usage("claude", "output", 0.15, "claude-3-sonnet_output")
        db.save_script(result)
        cb.record_success("claude")
        icon = "OK" if result.get("auto_approved") else "REVIEW"
        notify(notifier, f"{icon} Script Ready",
               f"Score: {score:.1f}/10 -- {result['quality']['word_count']} words\n{result['script'][:100]}...",
               "info" if score >= 8.5 else "warning", {"score": score})
        logger.info("Script generated", extra={
            "content_id": result["content_id"], "score": score, "status": result["status"],
        })
    return result


def step_video(notifier, cb, tracker):
    logger.info("STEP 3 — VIDEO PIPELINE")
    db = get_db()
    try:
        result = video_pipeline.run(CONFIG)
    except _TRANSIENT_ERRORS as e:
        logger.warning("Video step transient failure", extra={"error": str(e)})
        return None

    if result:
        if result.get("audio_path") and "_el.mp3" in str(result.get("audio_path", "")):
            tracker.record_usage("elevenlabs", "tts", len(result.get("script", "")))
        db.save_video(result)
        a = "OK" if result.get("audio_path") else "FAIL"
        v = "OK" if result.get("avatar_video_path") else "SKIP"
        f = "OK" if result.get("final_video_path") else "SKIP"
        notify(notifier, "Video Done", f"Audio {a} -- Avatar {v} -- Final {f}",
               "info", {"status": result.get("status")})
        logger.info("Video done", extra={
            "content_id": result.get("content_id"), "status": result.get("status"),
        })
    return result


def step_publish(notifier, cb, tracker):
    logger.info("STEP 4 — PUBLISHING")
    db = get_db()
    try:
        result = platform_publisher.run(CONFIG)
    except _TRANSIENT_ERRORS as e:
        logger.warning("Publish step transient failure", extra={"error": str(e)})
        return None

    if result:
        res = result.get("publish_results", {})
        db.save_publish_results(result.get("content_id", result["article"]["title"][:20]), res)
        lines = [
            f"{'OK' if r.get('success') else 'FAIL'} {p}: {(r.get('url') or r.get('error', ''))[:50]}"
            for p, r in res.items()
        ]
        ok = sum(1 for r in res.values() if r.get("success"))
        if ok > 0:
            tracker.increment_video_count()
        notify(notifier, "Published", "\n".join(lines),
               "info" if ok else "warning", {"platforms_ok": ok})
        logger.info("Published", extra={"content_id": result.get("content_id"), "platforms_ok": ok})
    return result


def step_analytics(notifier, tracker):
    logger.info("STEP 5 — ANALYTICS")
    db = get_db()
    try:
        result = analytics_tracker.run(CONFIG)
    except _TRANSIENT_ERRORS as e:
        logger.warning("Analytics step transient failure", extra={"error": str(e)})
        return None

    if result:
        s = result["summary"]
        for platform, data in result.get("platforms", {}).items():
            db.save_analytics_snapshot(platform, data)
        notify(notifier, "Analytics Updated",
               f"Views: {s['total_views_30d']:,} -- Followers: {s['total_followers']:,}", "info")
    return result

# ── Full pipeline ──────────────────────────────────────────────────────────

async def run_full_pipeline():
    start=datetime.now(timezone.utc); notifier=get_notifier(); tracker=CostTracker()
    cbc=CONFIG.get("error_handling",{}).get("circuit_breaker",{})
    cb=CircuitBreaker(cbc.get("failure_threshold",5),cbc.get("timeout_minutes",15))
    logger.info("Pipeline starting",extra={"version":"2.2","timestamp":start.isoformat()})
    notify(notifier,"⚡ Pipeline Starting",f"Bolt v2.2 — {start.strftime('%Y-%m-%d %H:%M UTC')}","info")

    if not await step_news(notifier, cb, tracker):
        notify(notifier,"🛑 Halted","No news available","error"); return

    script = step_script(notifier, cb, tracker)
    if not script:
        notify(notifier,"🛑 Halted","Script generation failed","error"); return

    # HITL gate -- non-blocking per pre-plan Rule 3 ("pipeline never blocks")
    # If pending_review: notify and EXIT. The human approves via dashboard/CLI,
    # which creates a video job. The job worker handles everything from here.
    if script.get("status") == "pending_review" and not CONFIG["automation"].get("auto_publish_enabled"):
        db = get_db()
        notify(notifier, "👁️ Awaiting Review",
               f"Script {script['content_id']} needs approval.\n"
               f"Dashboard: approve/reject in Content Management\n"
               f"CLI: python hitl.py approve {script['content_id']}",
               "warning", {"content_id": script["content_id"]})
        logger.info("Pipeline paused at HITL gate -- exiting (job worker will continue after approval)",
                     extra={"content_id": script["content_id"]})
        return  # EXIT -- scheduler does not wait. Approval creates a video job.

    # Auto-approved scripts continue immediately -- create video job
    db = get_db()

    # Hard budget check before expensive video rendering
    try:
        BudgetEnforcer(CONFIG).check_or_raise("video")
    except BudgetExceededError as e:
        notify(notifier, "🛑 Budget Hard Stop", str(e), "error")
        logger.error("Budget exceeded before video step", extra={"limit_type": e.limit_type, "spent": e.spent})
        # Defer to midnight instead of dropping
        db.enqueue_job("video", content_id=script["content_id"], max_attempts=3)
        logger.info("Video job deferred (budget exceeded)", extra={"content_id": script["content_id"]})
        return

    video = step_video(notifier, cb, tracker)
    if video and (video.get("video_ready") or video.get("audio_path")):
        step_publish(notifier, cb, tracker)
    else:
        notify(notifier, "⏭️ Publish Skipped", "No media produced", "warning")

    step_analytics(notifier, tracker)

    elapsed=(datetime.now(timezone.utc)-start).total_seconds()
    m=tracker.get_monthly_summary()
    notify(notifier,"✅ Pipeline Done",
           f"Finished in {elapsed:.0f}s\nMonth: ${m['total_cost']:.3f} · {m['videos']} videos","info")
    logger.info("Pipeline complete",extra={"elapsed_seconds":elapsed,"month_cost":m['total_cost']})

# ── Scheduler ─────────────────────────────────────────────────────────────

def run_scheduler():
    import schedule
    notifier=get_notifier(); backup=BackupSystem()
    logger.info("Scheduler started — 24/7 mode")
    notify(notifier,"🕐 Scheduler Started","Bolt v2.2 running 24/7","info")
    schedule.every().day.at("06:00").do(lambda: asyncio.run(run_full_pipeline()))
    schedule.every(6).hours.do(lambda: asyncio.run(news_aggregator.run(CONFIG)))
    schedule.every().day.at("09:00").do(lambda: analytics_tracker.run(CONFIG))
    schedule.every().day.at("03:00").do(lambda: backup.create_backup("daily"))
    schedule.every().monday.at("02:00").do(lambda: backup.create_backup("weekly"))
    # Feedback aggregator: weekly performance learning loop (pre-plan Section 22)
    try:
        from feedback_aggregator import aggregate as feedback_aggregate
        schedule.every().sunday.at("04:00").do(lambda: feedback_aggregate(CONFIG))
        logger.info("Feedback aggregator scheduled (Sundays 04:00 UTC)")
    except ImportError:
        logger.debug("feedback_aggregator not available -- skipping")
    schedule.every(30).days.at("01:00").do(lambda: backup.create_backup("monthly"))
    logger.info("Scheduler ready — pipeline at 06:00 UTC")
    while True: schedule.run_pending(); time.sleep(30)

# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    p=argparse.ArgumentParser(description="⚡ Bolt AI v2.2")
    p.add_argument("--step",choices=["news","script","video","publish","analytics"])
    p.add_argument("--backup",choices=["daily","weekly","monthly","manual"])
    p.add_argument("--cost-summary",action="store_true")
    p.add_argument("--list-backups",action="store_true")
    p.add_argument("--db-stats",action="store_true")
    p.add_argument("--secrets-audit",action="store_true")
    p.add_argument("--restore",metavar="BACKUP_ID")
    p.add_argument("--schedule",action="store_true")
    p.add_argument("--config",default="code/config.json")
    args=p.parse_args()

    notifier=get_notifier(); tracker=CostTracker(); cb=CircuitBreaker()

    if args.secrets_audit:
        secrets_audit()
    elif args.db_stats:
        from database import get_db
        get_db().get_dashboard_summary()
        import database; database.main()
    elif args.cost_summary:
        t=tracker; total=t.get_total_summary(); m=t.get_monthly_summary()
        print(f"\n{'═'*45}\n  ⚡ BOLT COST SUMMARY\n{'═'*45}")
        print(f"  Total:     ${total['total_spent']:.4f} · {total['total_videos']} videos")
        print(f"  Avg/video: ${total['avg_cost_per_video']:.4f}")
        print(f"  Month:     ${m['total_cost']:.4f} · {m['videos']} videos")
        for svc,cost in sorted(m.get("services",{}).items(),key=lambda x:x[1],reverse=True):
            print(f"    {svc:20s} ${cost:.4f}")
        print(f"{'═'*45}\n")
    elif args.list_backups:
        bs=BackupSystem(); backups=bs.list_backups()
        print(f"\n{'═'*55}\n  💾 BOLT BACKUPS\n{'═'*55}")
        for b in backups: print(f"  [{b['type']:8s}] {str(b['timestamp'])[:16]} · {b['size_mb']:.1f} MB")
        print(f"{'═'*55}\n")
    elif args.restore:
        ok=BackupSystem().restore_backup(args.restore)
        print(f"{'✅ Restored' if ok else '❌ Failed'}: {args.restore}")
    elif args.backup:
        r=BackupSystem().create_backup(args.backup)
        print(f"✅ Backup: {r['backup_id']} ({r['size_mb']} MB)")
    elif args.schedule:
        run_scheduler()
    elif args.step=="news":
        asyncio.run(step_news(notifier,cb,tracker))
    elif args.step=="script":
        step_script(notifier,cb,tracker)
    elif args.step=="video":
        step_video(notifier,cb,tracker)
    elif args.step=="publish":
        step_publish(notifier,cb,tracker)
    elif args.step=="analytics":
        step_analytics(notifier,tracker)
    else:
        asyncio.run(run_full_pipeline())

if __name__=="__main__": main()

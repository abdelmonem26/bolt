"""
Bolt AI -- Distribution Orchestrator (distribution_orchestrator.py)
===================================================================
Loads platform adapters, calls transform() + publish() on each,
writes Publication records to DB.

Pre-plan Section 23: "Takes content_id, reads Video and Script from DB,
instantiates configured platform adapters, calls transform() and publish()
on each, writes Publication records. Does not know which platforms it is
talking to."

Pre-plan boundary rule: "Distribution never imports from production pipeline
modules. They share only the database."
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("bolt.dist.orchestrator")


def distribute(content_id: str, config: dict) -> dict:
    """
    Distribute a completed video to all enabled platforms.

    Reads Video + Script from DB, runs each platform adapter's
    transform() and publish(), writes Publication records.

    Args:
        content_id: The content_id of the video to distribute
        config:     Full config dict with secrets injected

    Returns:
        Dict with per-platform results: {"youtube": {...}, "tiktok": {...}, ...}
    """
    from database import get_db

    db = get_db()
    video = db.get_video_status(content_id)
    if not video or not video.get("final_path"):
        logger.error("No assembled video found for distribution", extra={"content_id": content_id})
        return {"status": "failed", "error": "No video found"}

    # Read the script for metadata
    script = db.get_script_by_content_id(content_id)
    if not script:
        logger.error("No script found for distribution", extra={"content_id": content_id})
        return {"status": "failed", "error": "No script found"}

    # Build article dict from script data (distribution doesn't import from production)
    article = script.get("article") or {"title": script.get("script", "")[:60], "source": ""}
    master_video_path = video["final_path"]

    # Load configured adapters
    adapters = _load_adapters(config)
    if not adapters:
        logger.warning("No platform adapters configured", extra={"content_id": content_id})
        return {"status": "no_platforms"}

    results = {}
    # Pre-plan Section 21: "Never post to all three platforms within 10 minutes
    # of each other. Stagger by at least 2 hours."
    stagger_seconds = config.get("distribution", {}).get("stagger_seconds", 120 * 60)  # default 2h
    platforms_published = 0

    for adapter in adapters:
        platform = adapter.platform_name
        # Enforce stagger rule: wait between platform posts (skip for first)
        if platforms_published > 0 and stagger_seconds > 0:
            logger.info(
                f"Stagger rule: waiting {stagger_seconds}s before {platform}",
                extra={"content_id": content_id},
            )
            time.sleep(stagger_seconds)

        logger.info(f"Distributing to {platform}", extra={"content_id": content_id})

        try:
            # Transform master video for this platform
            package = adapter.transform(master_video_path, script, article, config)

            # Publish
            result = adapter.publish(package, config)

            # Write Publication record to DB
            db.save_publication(
                content_id=content_id,
                platform=platform,
                success=result.success,
                url=result.post_url,
                post_id=result.post_id,
                error_msg=result.error,
                scheduled_at=result.scheduled_at or datetime.now(timezone.utc).isoformat(),
            )

            results[platform] = {
                "success": result.success,
                "url": result.post_url,
                "post_id": result.post_id,
                "error": result.error,
            }

            platforms_published += 1

            if result.success:
                logger.info(f"Distribution OK: {platform}", extra={
                    "content_id": content_id, "url": result.post_url,
                })
            else:
                logger.warning(f"Distribution failed: {platform}", extra={
                    "content_id": content_id, "error": result.error,
                })
                # Create retry job for failed platform
                db.enqueue_job(f"distribute_{platform}", content_id=content_id, max_attempts=3)

        except Exception as e:
            logger.error(f"Distribution error: {platform}", extra={
                "content_id": content_id, "error": str(e),
            })
            results[platform] = {"success": False, "error": str(e)}

    # Schedule confirmation check 15 minutes from now
    try:
        db.enqueue_job("confirm_posts", content_id=content_id, max_attempts=3)
        logger.info("Confirmation check job created", extra={"content_id": content_id})
    except Exception:
        pass

    # Send notification summary
    _notify_results(content_id, results, config)

    return results


def _load_adapters(config: dict) -> list:
    """Load all enabled platform adapters."""
    adapters = []
    platforms = config.get("platforms", {})

    if platforms.get("youtube", {}).get("enabled"):
        try:
            from adapters.youtube import YouTubeAdapter
            adapter = YouTubeAdapter()
            if adapter.validate_credentials(config):
                adapters.append(adapter)
            else:
                logger.debug("YouTube adapter skipped -- credentials not configured")
        except ImportError:
            logger.warning("YouTube adapter not available")

    if platforms.get("tiktok", {}).get("enabled"):
        try:
            from adapters.tiktok import TikTokAdapter
            adapter = TikTokAdapter()
            if adapter.validate_credentials(config):
                adapters.append(adapter)
            else:
                logger.debug("TikTok adapter skipped -- credentials not configured")
        except ImportError:
            logger.warning("TikTok adapter not available")

    if platforms.get("instagram", {}).get("enabled"):
        try:
            from adapters.instagram import InstagramAdapter
            adapter = InstagramAdapter()
            if adapter.validate_credentials(config):
                adapters.append(adapter)
            else:
                logger.debug("Instagram adapter skipped -- credentials not configured")
        except ImportError:
            logger.warning("Instagram adapter not available")

    return adapters


def _notify_results(content_id: str, results: dict, config: dict) -> None:
    """Send a notification summarizing distribution results."""
    try:
        from notifications import NotificationManager, Notification, NotificationLevel
        ok = sum(1 for r in results.values() if r.get("success"))
        total = len(results)
        lines = [
            f"{'OK' if r.get('success') else 'FAIL'} {p}: {r.get('url') or r.get('error', '')[:50]}"
            for p, r in results.items()
        ]
        level = NotificationLevel.INFO if ok == total else NotificationLevel.WARNING
        nm = NotificationManager(config)
        nm.send(Notification(
            title=f"Distribution: {ok}/{total} platforms",
            message=f"Content: {content_id}\n" + "\n".join(lines),
            level=level,
            metadata={"content_id": content_id, "platforms_ok": ok},
        ))
    except Exception:
        pass

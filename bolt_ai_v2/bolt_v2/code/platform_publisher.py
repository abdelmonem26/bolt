#!/usr/bin/env python3
"""
Bolt AI — Platform Publisher v2
Publishes finished videos to YouTube Shorts, TikTok, and Instagram Reels.
Uses Buffer API for scheduling + direct platform APIs for YouTube.
Sends Discord notifications for every action.
"""

import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("bolt.publisher")


def load_config(path: str = "code/config.json") -> dict:
    """Legacy loader -- prefer shared_config.get_config() for secret injection."""
    from shared_config import get_config
    return get_config(path)


# ─────────────────────────────────────────────
# NOTIFICATIONS (uses centralized NotificationManager)
# ─────────────────────────────────────────────

def notify_discord(message: str, color: int, config: dict, fields: list = None) -> None:
    """Send a notification via the centralized notification system.
    Falls back to direct Discord webhook if NotificationManager is unavailable."""
    try:
        from notifications import NotificationManager, Notification, NotificationLevel
        level = NotificationLevel.INFO if color == 0x00FF00 else (
            NotificationLevel.WARNING if color == 0xFFAA00 else NotificationLevel.ERROR
        )
        nm = NotificationManager(config)
        metadata = {}
        if fields:
            metadata = {f["name"]: f["value"] for f in fields}
        nm.send(Notification(title="Bolt Automation", message=message, level=level, metadata=metadata))
    except Exception:
        # Direct fallback if notifications module is unavailable
        webhook_url = config.get("apis", {}).get("discord_webhook_url", "")
        if not webhook_url or webhook_url.startswith("YOUR_"):
            return
        embed = {
            "title": "Bolt Automation",
            "description": message,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "Bolt AI Content Creator"},
        }
        if fields:
            embed["fields"] = fields
        try:
            requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
        except Exception as e:
            logger.warning(f"Discord notify failed: {e}")


# ─────────────────────────────────────────────
# YOUTUBE DIRECT API
# ─────────────────────────────────────────────

def publish_youtube(video_url: str, package: dict, config: dict) -> dict:
    """
    Upload video to YouTube Shorts via YouTube Data API v3.
    Requires OAuth refresh token to generate access token first.
    """
    cfg = config["platforms"]["youtube"]
    captions = package.get("captions", {}).get("youtube", {})
    title = captions.get("title", package["article"]["title"])[:100]
    description = captions.get("description", "")
    hashtags = " ".join(captions.get("hashtags", ["#AI", "#AINews"]))
    tags = captions.get("tags", ["AI", "artificial intelligence"])

    # Step 1: Refresh access token
    token_resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": config["apis"]["youtube_client_id"],
        "client_secret": config["apis"]["youtube_client_secret"],
        "refresh_token": config["apis"]["youtube_refresh_token"],
        "grant_type": "refresh_token",
    })
    if not token_resp.ok:
        return {"platform": "youtube", "success": False, "error": "Token refresh failed"}
    access_token = token_resp.json()["access_token"]

    headers = {"Authorization": f"Bearer {access_token}"}

    # Step 2: Download video to temp file (from CDN URL)
    video_resp = requests.get(video_url, timeout=120, stream=True)
    temp_path = Path(f"/tmp/bolt_yt_{package['content_id']}.mp4")
    with open(temp_path, "wb") as f:
        for chunk in video_resp.iter_content(chunk_size=8192):
            f.write(chunk)

    # Step 3: Upload video
    metadata = {
        "snippet": {
            "title": f"{title} #Shorts",
            "description": f"{description}\n\n{hashtags}",
            "tags": tags,
            "categoryId": cfg.get("category_id", "28"),
        },
        "status": {
            "privacyStatus": cfg.get("privacy", "public"),
            "selfDeclaredMadeForKids": False,
        },
    }

    upload_url = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=multipart&part=snippet,status"
    from requests_toolbelt import MultipartEncoder
    mp = MultipartEncoder(fields={
        "metadata": ("metadata", json.dumps(metadata), "application/json"),
        "video": ("video.mp4", open(temp_path, "rb"), "video/mp4"),
    })

    try:
        resp = requests.post(upload_url, data=mp, headers={**headers, "Content-Type": mp.content_type}, timeout=300)
        resp.raise_for_status()
        video_id = resp.json()["id"]
        yt_url = f"https://www.youtube.com/shorts/{video_id}"
        logger.info(f"✅ YouTube Shorts published: {yt_url}")
        temp_path.unlink(missing_ok=True)
        return {"platform": "youtube", "success": True, "url": yt_url, "video_id": video_id}
    except Exception as e:
        logger.error(f"YouTube upload failed: {e}")
        temp_path.unlink(missing_ok=True)
        return {"platform": "youtube", "success": False, "error": str(e)}


# ─────────────────────────────────────────────
# BUFFER API (TikTok + Instagram + YouTube fallback)
# ─────────────────────────────────────────────

# Buffer helpers are now in buffer_utils.py (shared with distribution layer adapters).
# Re-export for backward compatibility with any code importing from here.
from buffer_utils import get_buffer_profile_ids, schedule_via_buffer  # noqa: F401


# ─────────────────────────────────────────────
# ORCHESTRATOR
# ─────────────────────────────────────────────

def publish_all_platforms(package: dict, config: dict) -> dict:
    """
    Publish the video to all enabled platforms.
    Uses YouTube Data API directly for YouTube, Buffer for TikTok + Instagram.
    """
    # Check both URL and path keys (video_pipeline stores _path, external CDNs store _url)
    video_url = (
        package.get("final_video_url")
        or package.get("final_video_path")
        or package.get("avatar_video_url")
        or package.get("avatar_video_path")
    )
    if not video_url:
        logger.error("No video URL/path in package — cannot publish")
        return {"success": False, "error": "No video URL/path available"}

    results = {}
    access_token = config["apis"].get("buffer_access_token", "")
    buffer_profiles = {}
    if access_token and not access_token.startswith("YOUR_"):
        try:
            buffer_profiles = get_buffer_profile_ids(access_token)
            logger.info(f"Buffer profiles found: {list(buffer_profiles.keys())}")
        except Exception as e:
            logger.warning(f"Could not fetch Buffer profiles: {e}")

    # YouTube — direct API (best for Shorts compliance)
    if config["platforms"]["youtube"]["enabled"]:
        yt_key = config["apis"].get("youtube_client_id", "")
        if yt_key and not yt_key.startswith("YOUR_"):
            results["youtube"] = publish_youtube(video_url, package, config)
        elif "youtube" in buffer_profiles:
            results["youtube"] = schedule_via_buffer(
                video_url, package, "youtube",
                buffer_profiles["youtube"], access_token,
                config["platforms"]["youtube"]["post_time"], config
            )
        else:
            results["youtube"] = {"platform": "youtube", "success": False, "error": "No YouTube credentials"}

    # TikTok — via Buffer
    if config["platforms"]["tiktok"]["enabled"]:
        if "tiktok" in buffer_profiles:
            results["tiktok"] = schedule_via_buffer(
                video_url, package, "tiktok",
                buffer_profiles["tiktok"], access_token,
                config["platforms"]["tiktok"]["post_time"], config
            )
        else:
            results["tiktok"] = {"platform": "tiktok", "success": False, "error": "No TikTok Buffer profile"}

    # Instagram — via Buffer
    if config["platforms"]["instagram"]["enabled"]:
        if "instagram" in buffer_profiles:
            results["instagram"] = schedule_via_buffer(
                video_url, package, "instagram",
                buffer_profiles["instagram"], access_token,
                config["platforms"]["instagram"]["post_time"], config
            )
        else:
            # Direct Instagram Graph API fallback
            results["instagram"] = publish_instagram_direct(video_url, package, config)

    # Discord notification
    success_count = sum(1 for r in results.values() if r.get("success"))
    total = len(results)
    color = 0x00FF00 if success_count == total else (0xFFAA00 if success_count > 0 else 0xFF0000)
    notify_discord(
        f"Published **{package['article']['title'][:60]}**",
        color, config,
        fields=[
            {"name": p.title(), "value": "✅ Published" if r.get("success") else f"❌ {r.get('error', 'Failed')}", "inline": True}
            for p, r in results.items()
        ]
    )

    return results


def publish_instagram_direct(video_url: str, package: dict, config: dict) -> dict:
    """Direct Instagram Graph API publishing as fallback."""
    access_token = config["apis"].get("instagram_access_token", "")
    user_id = config["apis"].get("instagram_user_id", "")
    if not access_token or access_token.startswith("YOUR_"):
        return {"platform": "instagram", "success": False, "error": "No Instagram credentials"}

    captions = package.get("captions", {}).get("instagram", {})
    caption = captions.get("caption", "") + " " + " ".join(captions.get("hashtags", []))

    # Step 1: Create media container
    container_resp = requests.post(
        f"https://graph.facebook.com/v19.0/{user_id}/media",
        params={
            "video_url": video_url,
            "caption": caption,
            "media_type": "REELS",
            "access_token": access_token,
        }, timeout=30
    )
    if not container_resp.ok:
        return {"platform": "instagram", "success": False, "error": container_resp.text[:200]}

    container_id = container_resp.json().get("id")
    if not container_id:
        return {"platform": "instagram", "success": False, "error": "No container ID returned"}

    # Step 2: Publish
    import time
    time.sleep(10)  # Give Instagram time to process the video
    publish_resp = requests.post(
        f"https://graph.facebook.com/v19.0/{user_id}/media_publish",
        params={"creation_id": container_id, "access_token": access_token},
        timeout=20
    )
    if not publish_resp.ok:
        return {"platform": "instagram", "success": False, "error": publish_resp.text[:200]}

    media_id = publish_resp.json().get("id", "")
    logger.info(f"✅ Instagram Reel published: media_id={media_id}")
    return {"platform": "instagram", "success": True, "media_id": media_id}


def run(config: "dict | None" = None, *, config_path: str = "code/config.json") -> "dict | None":
    """Load ready-to-publish package and publish to all platforms.

    Args:
        config: Pre-loaded config dict (preferred). When provided, config_path is ignored.
        config_path: Legacy fallback -- used only when config is None (CLI usage).
    """
    if config is None:
        config = load_config(config_path)
    queue_dir = Path(config["paths"]["queue"])

    ready = [f for f in sorted(queue_dir.glob("script_*.json"))
             if json.loads(f.read_text()).get("status") == "ready_to_publish"]

    if not ready:
        logger.warning("No videos ready to publish in queue.")
        return None

    package = json.loads(ready[0].read_text())
    logger.info(f"🚀 Publishing: {package['article']['title'][:60]}...")

    results = publish_all_platforms(package, config)

    # Update package status
    all_ok = all(r.get("success") for r in results.values())
    package["status"] = "published" if all_ok else "partial_publish"
    package["publish_results"] = results
    package["published_at"] = datetime.now(timezone.utc).isoformat()

    # Move to published directory
    published_dir = Path(config["paths"]["published"])
    published_dir.mkdir(parents=True, exist_ok=True)
    dest = published_dir / ready[0].name
    dest.write_text(json.dumps(package, indent=2, ensure_ascii=False))
    ready[0].unlink()

    logger.info(f"Publishing complete. Results: {json.dumps({k: v.get('success') for k, v in results.items()})}")
    return package


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run()
    if result:
        print(f"\n🚀 Published: {result['article']['title'][:60]}")
        for platform, r in result.get("publish_results", {}).items():
            icon = "✅" if r.get("success") else "❌"
            print(f"  {icon} {platform}: {r.get('url') or r.get('error', 'done')}")


# ════════════════════════════════════════════════════════════════════
# RESTORED FUNCTIONS — migrated from original PlatformPublisher class
# ════════════════════════════════════════════════════════════════════

def optimize_content_for_platforms(content: dict, config: dict) -> dict:
    """
    Generate platform-specific optimised content variants.
    Replaces PlatformPublisher.optimize_content_for_platforms() from v1.
    Returns a dict keyed by platform name with optimised title/description/hashtags.
    """
    script   = content.get("script", "")
    captions = content.get("captions", {})
    pillar   = content.get("pillar", "ai_news")
    article  = content.get("article", {})

    return {
        "youtube": {
            "title":       optimize_title(script, "youtube", config),
            "description": optimize_description(content, "youtube", config),
            "hashtags":    optimize_hashtags(generate_hashtags(content, config), "youtube", config),
            "tags":        captions.get("youtube", {}).get("tags", ["AI", "TechNews", "Shorts"]),
            "category_id": config.get("platforms", {}).get("youtube", {}).get("category_id", "28"),
        },
        "tiktok": {
            "caption":     captions.get("tiktok", {}).get("caption", "")
                           or optimize_description(content, "tiktok", config),
            "hashtags":    optimize_hashtags(generate_hashtags(content, config), "tiktok", config),
        },
        "instagram": {
            "caption":     captions.get("instagram", {}).get("caption", "")
                           or optimize_description(content, "instagram", config),
            "hashtags":    optimize_hashtags(generate_hashtags(content, config), "instagram", config),
        },
    }


def generate_hashtags(content: dict, config: dict) -> list[str]:
    """
    Generate relevant hashtags for a piece of content.
    Replaces PlatformPublisher.generate_hashtags() from v1.
    Uses the pillar-specific hashtag lists from config plus article keywords.
    """
    pillar = content.get("pillar", "ai_news")
    base   = config.get("hashtags", {}).get(pillar, ["#AI", "#TechNews"])

    # Extract additional keywords from the article title
    title  = content.get("article", {}).get("title", "")
    extra  = []
    keyword_map = {
        "openai":    "#OpenAI",    "gpt":         "#GPT",
        "claude":    "#Claude",    "anthropic":   "#Anthropic",
        "gemini":    "#Gemini",    "google":      "#Google",
        "meta":      "#Meta",      "llama":       "#LLaMA",
        "chatgpt":   "#ChatGPT",   "open source": "#OpenSource",
        "free":      "#FreeAI",    "robot":       "#Robotics",
    }
    for kw, tag in keyword_map.items():
        if kw in title.lower() and tag not in base:
            extra.append(tag)

    return list(dict.fromkeys(base + extra[:3]))  # Deduplicated, max 3 extras


def optimize_title(script: str, platform: str, config: dict) -> str:
    """
    Create a platform-optimised video title from the script.
    Replaces PlatformPublisher.optimize_title() from v1.
    """
    limit = config.get("platforms", {}).get(platform, {}).get("max_title_length", 100)
    # Use first sentence of script as base title
    first_sentence = script.split(".")[0].strip()[:80]
    if platform == "youtube":
        title = f"{first_sentence} #Shorts"
    elif platform == "tiktok":
        title = first_sentence
    else:
        title = first_sentence
    return title[:limit]


def optimize_description(content: dict, platform: str, config: dict) -> str:
    """
    Create a platform-optimised description/caption.
    Replaces PlatformPublisher.optimize_description() from v1.
    """
    script  = content.get("script", "")
    article = content.get("article", {})
    source  = article.get("source", "")
    link    = article.get("link", "")
    pillar  = content.get("pillar", "ai_news")
    hashtags = " ".join(
        optimize_hashtags(generate_hashtags(content, config), platform, config)
    )

    limit = {
        "youtube":   config.get("platforms",{}).get("youtube",{}).get("max_title_length", 5000),
        "tiktok":    config.get("platforms",{}).get("tiktok",{}).get("max_description_length", 2200),
        "instagram": config.get("platforms",{}).get("instagram",{}).get("max_caption_length", 2200),
    }.get(platform, 2200)

    if platform == "youtube":
        base = f"{script[:200]}...\n\nSource: {source}\n{link}\n\n{hashtags}\n\nSubscribe for daily AI news! ⚡"
    else:
        base = f"⚡ {script[:120]}... | Follow @BoltAI for daily AI updates!\n\n{hashtags}"

    return base[:limit]


def optimize_hashtags(hashtags: list[str], platform: str, config: dict) -> list[str]:
    """
    Trim and order hashtags for a specific platform's limits.
    Replaces PlatformPublisher.optimize_hashtags() from v1.
    """
    limit = config.get("platforms", {}).get(platform, {}).get("hashtag_limit", 15)
    # Always lead with highest-signal hashtags
    priority = ["#AI", "#ArtificialIntelligence", "#TechNews"]
    rest     = [h for h in hashtags if h not in priority]
    ordered  = priority + rest
    return ordered[:limit]


async def schedule_publishing(content: dict, scheduled_times: dict, config: dict) -> dict:
    """
    Schedule content for publishing at specified times per platform.
    Replaces PlatformPublisher.schedule_publishing() from v1.
    Uses Buffer API for scheduling.
    """
    import asyncio
    results = {}
    tasks   = []
    for platform, time_str in scheduled_times.items():
        tasks.append(_schedule_single_platform(content, platform, time_str, config))
    completed = await asyncio.gather(*tasks, return_exceptions=True)
    for i, platform in enumerate(scheduled_times):
        results[platform] = completed[i] if not isinstance(completed[i], Exception) else {"error": str(completed[i])}
    return results


async def _schedule_single_platform(content: dict, platform: str,
                                     scheduled_time: str, config: dict) -> dict:
    """
    Schedule a single platform post.
    Replaces PlatformPublisher._schedule_single_platform() from v1.
    """
    import asyncio
    logger.info(f"Scheduling {platform} at {scheduled_time}")
    # Delegates to the existing Buffer scheduling logic in schedule_via_buffer()
    access_token = config.get("apis", {}).get("buffer_access_token", "")
    if not access_token or access_token.startswith("→"):
        return {"platform": platform, "success": False, "error": "Buffer not configured"}
    try:
        profiles = get_buffer_profile_ids(access_token)
        if platform in profiles:
            return schedule_via_buffer(
                content.get("final_video_url", ""),
                content, platform,
                profiles[platform], access_token,
                scheduled_time, config
            )
        return {"platform": platform, "success": False, "error": "Platform not in Buffer profiles"}
    except Exception as e:
        return {"platform": platform, "success": False, "error": str(e)}


def get_analytics_summary(days: int = 7, config: dict = None) -> dict:
    """
    Get a quick analytics summary for post-publish performance tracking.
    Replaces PlatformPublisher.get_analytics_summary() from v1.
    """
    try:
        from analytics_tracker import run as run_analytics
        return run_analytics() or {}
    except Exception as e:
        logger.warning(f"get_analytics_summary failed: {e}")
        return {}

"""
Bolt AI -- Buffer API Utilities (buffer_utils.py)
===================================================
Shared helper functions for interacting with the Buffer API.

Extracted from platform_publisher.py so that both the production pipeline
(platform_publisher) and the distribution layer (adapters/) can use Buffer
without the distribution layer importing from production modules.

Pre-plan boundary rule: "Distribution never imports from production pipeline
modules. They share only the database."
"""

import logging
from datetime import datetime, timezone, timedelta

import requests

logger = logging.getLogger("bolt.buffer")


def get_buffer_profile_ids(access_token: str) -> dict:
    """Fetch Buffer profile IDs for YouTube, TikTok, Instagram.

    Returns:
        Dict mapping platform name to Buffer profile ID,
        e.g. {"youtube": "abc123", "tiktok": "def456"}.
    """
    resp = requests.get(
        "https://api.bufferapp.com/1/profiles.json",
        params={"access_token": access_token},
        timeout=10,
    )
    profiles = {}
    for p in resp.json():
        service = p.get("service", "").lower()
        if service in ("youtube", "tiktok", "instagram"):
            profiles[service] = p["id"]
    return profiles


def schedule_via_buffer(video_url: str, package: dict, platform: str,
                        profile_id: str, access_token: str, post_time: str,
                        config: dict) -> dict:
    """Schedule a video post via Buffer API.

    Args:
        video_url:    Path or URL to the video file.
        package:      Dict containing captions and article metadata.
        platform:     One of "youtube", "tiktok", "instagram".
        profile_id:   Buffer profile ID for the target platform.
        access_token: Buffer API access token.
        post_time:    Scheduled post time in "HH:MM" format.
        config:       Full config dict.

    Returns:
        Dict with "success" bool and either "buffer_id"/"scheduled_at" or "error".
    """
    captions = package.get("captions", {}).get(platform, {})
    hashtags = " ".join(captions.get("hashtags", ["#AI"]))

    if platform == "youtube":
        text = f"{captions.get('title', package['article']['title'])} {hashtags}"
    elif platform == "tiktok":
        text = captions.get("caption", f"⚡ {package['article']['title'][:100]}") + f" {hashtags}"
    else:  # instagram
        text = captions.get("caption", f"⚡ {package['article']['title'][:120]}") + f" {hashtags}"

    # Calculate scheduled time
    today = datetime.now(timezone.utc)
    hour, minute = map(int, post_time.split(":"))
    scheduled = today.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if scheduled < today:
        scheduled += timedelta(days=1)

    payload = {
        "profile_ids[]": profile_id,
        "text": text,
        "scheduled_at": scheduled.isoformat(),
        "media[video]": video_url,
        "media[thumbnail]": "",     # Buffer will auto-generate
        "access_token": access_token,
    }

    try:
        resp = requests.post(
            "https://api.bufferapp.com/1/updates/create.json",
            data=payload, timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success"):
            post_url = data.get("updates", [{}])[0].get("id", "")
            logger.info(f"Buffer scheduled [{platform}] for {scheduled.strftime('%H:%M UTC')}")
            return {"platform": platform, "success": True, "scheduled_at": scheduled.isoformat(),
                    "buffer_id": post_url}
        return {"platform": platform, "success": False, "error": data.get("message", "Unknown")}
    except Exception as e:
        logger.error(f"Buffer [{platform}] failed: {e}")
        return {"platform": platform, "success": False, "error": str(e)}

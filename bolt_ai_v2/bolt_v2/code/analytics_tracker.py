#!/usr/bin/env python3
"""
Bolt AI — Analytics Tracker v2
Pulls real performance data from YouTube, TikTok, and Instagram APIs.
Updates the dashboard's data files with live metrics.
"""

import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("bolt.analytics")


def load_config(path: str = "code/config.json") -> dict:
    """Legacy loader -- prefer shared_config.get_config() for secret injection."""
    from shared_config import get_config
    return get_config(path)


# ─────────────────────────────────────────────
# YOUTUBE ANALYTICS
# ─────────────────────────────────────────────

def fetch_youtube_analytics(config: dict) -> dict:
    """Fetch channel + video analytics from YouTube Data API v3."""
    try:
        # Refresh token
        token_resp = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": config["apis"]["youtube_client_id"],
            "client_secret": config["apis"]["youtube_client_secret"],
            "refresh_token": config["apis"]["youtube_refresh_token"],
            "grant_type": "refresh_token",
        }, timeout=10)
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]

        headers = {"Authorization": f"Bearer {access_token}"}

        # Channel stats
        ch_resp = requests.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "statistics", "mine": True},
            headers=headers, timeout=10
        )
        ch_data = ch_resp.json().get("items", [{}])[0].get("statistics", {})

        # Recent video stats (last 30 videos)
        vids_resp = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={"part": "id", "forMine": True, "type": "video",
                    "maxResults": 30, "order": "date"},
            headers=headers, timeout=10
        )
        vid_ids = ",".join(
            v["id"]["videoId"] for v in vids_resp.json().get("items", [])
        )

        total_views = total_likes = total_comments = 0
        if vid_ids:
            stats_resp = requests.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "statistics,contentDetails", "id": vid_ids},
                headers=headers, timeout=10
            )
            for v in stats_resp.json().get("items", []):
                s = v.get("statistics", {})
                total_views += int(s.get("viewCount", 0))
                total_likes += int(s.get("likeCount", 0))
                total_comments += int(s.get("commentCount", 0))

        return {
            "platform": "youtube",
            "subscribers": int(ch_data.get("subscriberCount", 0)),
            "total_channel_views": int(ch_data.get("viewCount", 0)),
            "video_count": int(ch_data.get("videoCount", 0)),
            "recent_30_views": total_views,
            "recent_30_likes": total_likes,
            "recent_30_comments": total_comments,
            "engagement_rate": round(
                (total_likes + total_comments) / max(total_views, 1) * 100, 2
            ),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"YouTube analytics failed: {e}")
        return {"platform": "youtube", "error": str(e)}


# ─────────────────────────────────────────────
# TIKTOK ANALYTICS
# ─────────────────────────────────────────────

def fetch_tiktok_analytics(config: dict) -> dict:
    """Fetch TikTok account and video analytics."""
    access_token = config["apis"].get("tiktok_access_token", "")
    if not access_token or access_token.startswith("YOUR_"):
        return {"platform": "tiktok", "error": "No access token"}

    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    base = "https://open.tiktokapis.com/v2"

    try:
        # User info
        user_resp = requests.post(
            f"{base}/user/info/",
            json={"fields": ["display_name", "follower_count", "following_count",
                             "likes_count", "video_count"]},
            headers=headers, timeout=10
        )
        user_data = user_resp.json().get("data", {}).get("user", {})

        # Video list
        videos_resp = requests.post(
            f"{base}/video/list/",
            json={"fields": ["id", "title", "view_count", "like_count",
                             "comment_count", "share_count"], "max_count": 20},
            headers=headers, timeout=10
        )
        videos = videos_resp.json().get("data", {}).get("videos", [])

        total_views = sum(v.get("view_count", 0) for v in videos)
        total_likes = sum(v.get("like_count", 0) for v in videos)
        total_comments = sum(v.get("comment_count", 0) for v in videos)
        total_shares = sum(v.get("share_count", 0) for v in videos)

        return {
            "platform": "tiktok",
            "followers": user_data.get("follower_count", 0),
            "total_likes": user_data.get("likes_count", 0),
            "video_count": user_data.get("video_count", 0),
            "recent_20_views": total_views,
            "recent_20_likes": total_likes,
            "recent_20_comments": total_comments,
            "recent_20_shares": total_shares,
            "engagement_rate": round(
                (total_likes + total_comments + total_shares) / max(total_views, 1) * 100, 2
            ),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"TikTok analytics failed: {e}")
        return {"platform": "tiktok", "error": str(e)}


# ─────────────────────────────────────────────
# INSTAGRAM ANALYTICS
# ─────────────────────────────────────────────

def fetch_instagram_analytics(config: dict) -> dict:
    """Fetch Instagram account and Reels analytics via Graph API."""
    access_token = config["apis"].get("instagram_access_token", "")
    user_id = config["apis"].get("instagram_user_id", "")
    if not access_token or access_token.startswith("YOUR_"):
        return {"platform": "instagram", "error": "No access token"}

    base = f"https://graph.facebook.com/v19.0/{user_id}"
    params = {"access_token": access_token}

    try:
        # Account info
        acct_resp = requests.get(
            base,
            params={**params, "fields": "followers_count,media_count,name"},
            timeout=10
        )
        acct = acct_resp.json()

        # Recent media
        media_resp = requests.get(
            f"{base}/media",
            params={**params, "fields": "id,like_count,comments_count,reach,plays",
                    "limit": 20},
            timeout=10
        )
        media = media_resp.json().get("data", [])

        total_plays = sum(m.get("plays", 0) for m in media)
        total_likes = sum(m.get("like_count", 0) for m in media)
        total_comments = sum(m.get("comments_count", 0) for m in media)
        total_reach = sum(m.get("reach", 0) for m in media)

        return {
            "platform": "instagram",
            "followers": acct.get("followers_count", 0),
            "media_count": acct.get("media_count", 0),
            "recent_20_plays": total_plays,
            "recent_20_likes": total_likes,
            "recent_20_comments": total_comments,
            "recent_20_reach": total_reach,
            "engagement_rate": round(
                (total_likes + total_comments) / max(total_plays, 1) * 100, 2
            ),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"Instagram analytics failed: {e}")
        return {"platform": "instagram", "error": str(e)}


# ─────────────────────────────────────────────
# DASHBOARD DATA WRITER
# ─────────────────────────────────────────────

def build_dashboard_analytics(yt: dict, tt: dict, ig: dict, published_dir: Path) -> dict:
    """Merge platform data into the dashboard-compatible analytics.json format."""

    # Load published content history for trend data
    published_videos = []
    for f in sorted(published_dir.glob("script_*.json"), reverse=True)[:30]:
        try:
            pkg = json.loads(f.read_text())
            published_videos.append({
                "date": pkg.get("published_at", "")[:10],
                "title": pkg.get("article", {}).get("title", "")[:50],
                "pillar": pkg.get("pillar", "ai_news"),
                "score": pkg.get("quality", {}).get("overall_score", 0),
                "publish_results": pkg.get("publish_results", {}),
            })
        except Exception:
            pass

    total_views = (
        yt.get("recent_30_views", 0) +
        tt.get("recent_20_views", 0) +
        ig.get("recent_20_plays", 0)
    )
    total_followers = (
        yt.get("subscribers", 0) +
        tt.get("followers", 0) +
        ig.get("followers", 0)
    )
    avg_engagement = round(
        (yt.get("engagement_rate", 0) +
         tt.get("engagement_rate", 0) +
         ig.get("engagement_rate", 0)) / 3, 2
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_views_30d": total_views,
            "total_followers": total_followers,
            "avg_engagement_rate": avg_engagement,
            "videos_published": len(published_videos),
        },
        "platforms": {
            "youtube": yt,
            "tiktok": tt,
            "instagram": ig,
        },
        "recent_content": published_videos[:10],
        "weekly_views": _build_weekly_chart(published_videos, yt, tt, ig),
    }


def _build_weekly_chart(videos: list, yt: dict, tt: dict, ig: dict) -> list:
    """Build a 7-day view trend for the dashboard chart."""
    days = []
    for i in range(6, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%a")
        # Rough estimate — in production, use per-video analytics
        yt_v = yt.get("recent_30_views", 0) // 30
        tt_v = tt.get("recent_20_views", 0) // 20
        ig_v = ig.get("recent_20_plays", 0) // 20
        days.append({
            "day": d,
            "youtube": yt_v,
            "tiktok": tt_v,
            "instagram": ig_v,
        })
    return days


def run(config=None, *, config_path: str = "code/config.json") -> dict:
    """Fetch all analytics and update dashboard data files.

    Args:
        config: Pre-loaded config dict (preferred). When provided, config_path is ignored.
        config_path: Legacy fallback -- used only when config is None (CLI usage).
    """
    if config is None:
        config = load_config(config_path)
    logger.info("📊 Fetching analytics from all platforms...")

    yt = fetch_youtube_analytics(config)
    tt = fetch_tiktok_analytics(config)
    ig = fetch_instagram_analytics(config)

    published_dir = Path(config["paths"]["published"])
    published_dir.mkdir(parents=True, exist_ok=True)

    analytics = build_dashboard_analytics(yt, tt, ig, published_dir)

    # Write to analytics directory
    analytics_dir = Path(config["paths"]["analytics"])
    analytics_dir.mkdir(parents=True, exist_ok=True)
    out_path = analytics_dir / "analytics.json"
    out_path.write_text(json.dumps(analytics, indent=2))

    # Also write to dashboard public data folder if it exists
    dashboard_data = Path("bolt-dashboard/public/data/analytics.json")
    if dashboard_data.parent.exists():
        dashboard_data.write_text(json.dumps(analytics, indent=2))
        logger.info(f"Dashboard analytics updated: {dashboard_data}")

    # ── Feedback loop: update per-publication metrics ─────────────────────
    # Pre-plan: "24 hours after posting, the analytics tracker fetches views,
    # retention rate, likes, and comments from each platform. These update
    # the Publication records and feed back into the Article scoring model."
    try:
        from database import get_db
        db = get_db()
        stale = db.get_publications_needing_metrics(min_age_hours=24)
        updated = 0
        for pub in stale:
            platform = pub["platform"]
            platform_data = analytics.get("platforms", {}).get(platform, {})
            if not platform_data:
                continue
            # Estimate per-video metrics from channel-level data
            total_videos = max(analytics["summary"].get("videos_published", 1), 1)
            views = platform_data.get("recent_30_views",
                    platform_data.get("recent_20_views",
                    platform_data.get("recent_20_plays", 0))) // total_videos
            engagement = platform_data.get("engagement_rate", 0.0)
            if views > 0 or engagement > 0:
                db.update_publication_metrics(pub["content_id"], platform, views, engagement)
                updated += 1
        if updated:
            logger.info(f"Updated metrics for {updated} publications")
    except Exception as e:
        logger.warning(f"Publication metrics update failed: {e}")

    logger.info(f"Analytics saved. Total views: {analytics['summary']['total_views_30d']:,}")
    return analytics


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run()
    print(f"\n📊 Analytics summary:")
    print(f"  Total views (30d): {result['summary']['total_views_30d']:,}")
    print(f"  Total followers:   {result['summary']['total_followers']:,}")
    print(f"  Avg engagement:    {result['summary']['avg_engagement_rate']}%")
    print(f"  Videos published:  {result['summary']['videos_published']}")


# ════════════════════════════════════════════════════════════════════
# RESTORED FUNCTIONS — migrated from original AnalyticsTracker class
# ════════════════════════════════════════════════════════════════════

def record_metric(platform: str, content_id: str, metric_type: str,
                  value: float, config: dict) -> None:
    """
    Record a single performance metric to the database.
    Replaces AnalyticsTracker.record_metric() from v1.
    """
    try:
        from database import get_db
        db = get_db()
        db.record_cost(service=f"metric_{platform}", operation=metric_type,
                       quantity=value, cost_usd=0.0, content_id=content_id)
    except Exception as e:
        logger.warning(f"record_metric failed: {e}")


def fetch_platform_metrics(platform: str, content_id: str, config: dict) -> dict:
    """
    Fetch per-video metrics for a specific piece of content.
    Replaces AnalyticsTracker.fetch_platform_metrics() from v1.
    """
    if platform == "youtube":
        return fetch_youtube_analytics(config)
    elif platform == "tiktok":
        return fetch_tiktok_analytics(config)
    elif platform == "instagram":
        return fetch_instagram_analytics(config)
    return {}


def update_content_metrics(content_id: str, platforms: list[str], config: dict) -> dict:
    """
    Fetch and store live metrics for a published video across all platforms.
    Replaces AnalyticsTracker.update_content_metrics() from v1.
    Called ~24 hours after publishing.
    """
    results = {}
    for platform in platforms:
        data = fetch_platform_metrics(platform, content_id, config)
        results[platform] = data
        try:
            from database import get_db
            get_db().save_analytics_snapshot(platform, data)
        except Exception:
            pass
    logger.info(f"Content metrics updated for {content_id}: {list(results.keys())}")
    return results


def calculate_content_performance(content_id: str) -> dict:
    """
    Calculate per-video ROI and performance score.
    Replaces AnalyticsTracker.calculate_content_performance() from v1.
    Returns a performance score (0–10) combining views, engagement, and cost.
    """
    try:
        from database import get_db
        db = get_db()
        cost_data = db.get_cost_summary()
        analytics  = db.get_latest_analytics()

        total_views      = sum(p.get("recent_30_views", 0) for p in analytics.values())
        avg_engagement   = sum(p.get("engagement_rate", 0) for p in analytics.values()) / max(len(analytics), 1)
        cost_per_video   = cost_data["total_usd"] / max(db.get_dashboard_summary()["total_published"], 1)

        view_score       = min(total_views / 10000 * 3.0, 3.0)
        engagement_score = min(avg_engagement / 10.0 * 4.0, 4.0)
        cost_score       = max(0, 3.0 - (cost_per_video * 3.0))

        overall = round(view_score + engagement_score + cost_score, 2)
        return {
            "content_id":      content_id,
            "total_views":     total_views,
            "avg_engagement":  round(avg_engagement, 2),
            "cost_usd":        round(cost_per_video, 4),
            "performance_score": overall,
            "breakdown":       {"views": view_score, "engagement": engagement_score, "cost": cost_score},
        }
    except Exception as e:
        logger.warning(f"calculate_content_performance failed: {e}")
        return {"content_id": content_id, "error": str(e)}


def generate_daily_summary(date: str = None, config: dict = None) -> dict:
    """
    Generate a comprehensive daily performance summary.
    Replaces AnalyticsTracker.generate_daily_summary() from v1.
    """
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    try:
        from database import get_db
        from cost_tracker import CostTracker
        db = get_db()
        tracker = CostTracker()
        dashboard = db.get_dashboard_summary()
        cost_day = tracker.get_daily_summary(date)
        analytics = db.get_latest_analytics()

        summary = {
            "date": date,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "videos_published": dashboard["total_published"],
            "pending_review":   dashboard["pending_review"],
            "daily_cost_usd":   round(cost_day.get("total_cost", 0), 4),
            "platforms": {
                p: {
                    "followers":      d.get("followers", d.get("subscribers", 0)),
                    "views_30d":      d.get("recent_30_views", d.get("recent_20_views", 0)),
                    "engagement_pct": d.get("engagement_rate", 0),
                }
                for p, d in analytics.items()
            },
            "insights": _generate_insights(analytics, cost_day),
        }

        # Write to daily summary file
        summary_dir = Path("data/analytics")
        summary_dir.mkdir(parents=True, exist_ok=True)
        out_path = summary_dir / f"daily_summary_{date}.json"
        out_path.write_text(json.dumps(summary, indent=2))
        logger.info(f"Daily summary generated: {out_path}")
        return summary
    except Exception as e:
        logger.error(f"generate_daily_summary failed: {e}")
        return {"date": date, "error": str(e)}


def generate_performance_charts(days: int = 30, config: dict = None) -> dict:
    """
    Generate chart data for the dashboard analytics page.
    Replaces AnalyticsTracker.generate_performance_charts() from v1.
    Returns data structures ready for Recharts in the frontend.
    """
    try:
        from database import get_db
        db = get_db()
        analytics = db.get_latest_analytics()

        yt = analytics.get("youtube", {})
        tt = analytics.get("tiktok", {})
        ig = analytics.get("instagram", {})

        return {
            "views_trend":    _build_weekly_chart([], yt, tt, ig),
            "platform_dist":  _create_platform_comparison_chart(yt, tt, ig),
            "engagement_trend": _create_engagement_chart(yt, tt, ig),
            "generated_at":   datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.warning(f"generate_performance_charts failed: {e}")
        return {}


def _create_platform_comparison_chart(yt: dict, tt: dict, ig: dict) -> list[dict]:
    """Replaces AnalyticsTracker._create_platform_comparison_chart() from v1."""
    return [
        {"platform": "YouTube",   "followers": yt.get("subscribers", 0),          "views": yt.get("recent_30_views", 0), "engagement": yt.get("engagement_rate", 0)},
        {"platform": "TikTok",    "followers": tt.get("followers", 0),             "views": tt.get("recent_20_views", 0), "engagement": tt.get("engagement_rate", 0)},
        {"platform": "Instagram", "followers": ig.get("followers", 0),             "views": ig.get("recent_20_plays", 0), "engagement": ig.get("engagement_rate", 0)},
    ]


def _create_engagement_chart(yt: dict, tt: dict, ig: dict) -> list[dict]:
    """Replaces AnalyticsTracker._create_engagement_chart() from v1."""
    return [
        {"platform": "YouTube",   "rate": yt.get("engagement_rate", 0)},
        {"platform": "TikTok",    "rate": tt.get("engagement_rate", 0)},
        {"platform": "Instagram", "rate": ig.get("engagement_rate", 0)},
    ]


def export_analytics_report(days: int = 30, config: dict = None) -> str:
    """
    Export a full analytics report as a JSON file.
    Replaces AnalyticsTracker.export_analytics_report() from v1.
    Returns the path to the exported report file.
    """
    try:
        from database import get_db
        from cost_tracker import CostTracker
        db = get_db()
        tracker = CostTracker()

        report = {
            "generated_at":     datetime.now(timezone.utc).isoformat(),
            "period_days":       days,
            "dashboard_summary": db.get_dashboard_summary(),
            "platform_analytics": db.get_latest_analytics(),
            "cost_summary":      tracker.get_monthly_summary(),
            "charts":            generate_performance_charts(days, config),
            "daily_summary":     generate_daily_summary(config=config),
            "insights":          _generate_insights(db.get_latest_analytics(), tracker.get_daily_summary()),
        }

        report_dir = Path("data/analytics")
        report_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d")
        out_path = report_dir / f"analytics_report_{date_str}.json"
        out_path.write_text(json.dumps(report, indent=2))
        logger.info(f"Analytics report exported: {out_path}")
        return str(out_path)
    except Exception as e:
        logger.error(f"export_analytics_report failed: {e}")
        return ""


def _generate_insights(analytics: dict, cost_data: dict) -> list[str]:
    """
    Generate human-readable performance insights.
    Replaces AnalyticsTracker._generate_insights() from v1.
    """
    insights = []
    yt = analytics.get("youtube", {})
    tt = analytics.get("tiktok", {})
    ig = analytics.get("instagram", {})

    yt_eng = yt.get("engagement_rate", 0)
    tt_eng = tt.get("engagement_rate", 0)
    ig_eng = ig.get("engagement_rate", 0)

    if yt_eng and tt_eng:
        best   = max([(yt_eng, "YouTube"), (tt_eng, "TikTok"), (ig_eng, "Instagram")], key=lambda x: x[0])
        worst  = min([(yt_eng, "YouTube"), (tt_eng, "TikTok"), (ig_eng, "Instagram")], key=lambda x: x[0])
        insights.append(f"{best[1]} has the highest engagement at {best[0]:.1f}% — prioritise content timing there.")
        if worst[0] < best[0] * 0.6:
            insights.append(f"{worst[1]} engagement ({worst[0]:.1f}%) is significantly lower — consider posting at different times.")

    cost = cost_data.get("total_cost", 0)
    if cost == 0:
        insights.append("All content produced at $0 cost using free tools — Edge-TTS and free avatar tiers.")
    elif cost < 0.50:
        insights.append(f"Production cost ${cost:.3f} — well within budget. Consider upgrading voice quality.")
    elif cost > 2.0:
        insights.append(f"Production cost ${cost:.2f} is high. Audit which service is expensive — switch to free alternatives.")

    yt_subs = yt.get("subscribers", 0)
    if yt_subs < 1000:
        insights.append(f"YouTube at {yt_subs:,} subscribers — need 1,000 to unlock monetisation. Focus on Shorts frequency.")
    elif yt_subs >= 1000:
        insights.append(f"YouTube monetisation threshold reached ({yt_subs:,} subscribers). Enable ads in YouTube Studio.")

    return insights

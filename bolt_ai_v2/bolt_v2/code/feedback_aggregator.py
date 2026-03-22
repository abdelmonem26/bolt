"""
Bolt AI -- Feedback Aggregator (feedback_aggregator.py)
========================================================
Weekly job that reads all Publication records with metrics, calculates
performance patterns, and writes performance_config overrides that the
distribution orchestrator and caption composer read on next run.

Pre-plan Section 22 (Distribution Growth Loop):
  Post -> Measure -> Learn -> Adjust -> Post again.
  - Best content pillar per platform
  - Best posting time per platform
  - Best hook pattern correlation with completion rate
  - Best-performing hashtags per pillar

Pre-plan Section 23: "Writes a performance_config override to DB.
Distribution orchestrator and caption composer read these overrides."
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

logger = logging.getLogger("bolt.feedback")


def aggregate(config: dict) -> dict:
    """Run the weekly feedback aggregation.

    Reads all publications with metrics, calculates:
    - Best content pillar per platform (by avg views)
    - Best posting time slots (by avg engagement)
    - Hashtag performance per pillar

    Writes results to the performance_overrides DB table.

    Args:
        config: Full config dict with secrets injected.

    Returns:
        Dict with computed overrides and counts.
    """
    from database import get_db
    db = get_db()

    # Get all publications with metrics
    with db._get_conn_ctx() as conn:
        rows = conn.execute("""
            SELECT p.content_id, p.platform, p.views, p.engagement_rate,
                   p.published_at, s.pillar, s.hook_strength, s.overall_score,
                   s.captions_json
            FROM publications p
            JOIN scripts s ON s.content_id = p.content_id
            WHERE p.success = 1 AND p.views > 0
            ORDER BY p.published_at DESC
            LIMIT 200
        """).fetchall()

    if len(rows) < 10:
        logger.info(f"Not enough data for feedback aggregation ({len(rows)} publications, need 10+)")
        return {"status": "insufficient_data", "count": len(rows)}

    publications = [dict(r) for r in rows]
    overrides = {}

    # 1. Best pillar per platform
    pillar_views = defaultdict(lambda: defaultdict(list))
    for pub in publications:
        pillar_views[pub["platform"]][pub["pillar"] or "ai_news"].append(pub["views"])

    best_pillars = {}
    for platform, pillars in pillar_views.items():
        avg_by_pillar = {p: sum(v) / len(v) for p, v in pillars.items() if v}
        if avg_by_pillar:
            best = max(avg_by_pillar, key=avg_by_pillar.get)
            best_pillars[platform] = {"pillar": best, "avg_views": round(avg_by_pillar[best], 1)}
    overrides["best_pillar_per_platform"] = best_pillars

    # 2. Best posting hour per platform
    hour_engagement = defaultdict(lambda: defaultdict(list))
    for pub in publications:
        if pub["published_at"]:
            try:
                hour = datetime.fromisoformat(pub["published_at"]).hour
                hour_engagement[pub["platform"]][hour].append(pub["engagement_rate"] or 0)
            except (ValueError, TypeError):
                pass

    best_hours = {}
    for platform, hours in hour_engagement.items():
        avg_by_hour = {h: sum(e) / len(e) for h, e in hours.items() if e}
        if avg_by_hour:
            best_h = max(avg_by_hour, key=avg_by_hour.get)
            best_hours[platform] = {"hour": best_h, "avg_engagement": round(avg_by_hour[best_h], 3)}
    overrides["best_hour_per_platform"] = best_hours

    # 3. Hook strength correlation with views
    hook_bins = {"high": [], "medium": [], "low": []}
    for pub in publications:
        hs = pub.get("hook_strength") or 0
        if hs >= 8:
            hook_bins["high"].append(pub["views"])
        elif hs >= 6:
            hook_bins["medium"].append(pub["views"])
        else:
            hook_bins["low"].append(pub["views"])

    hook_perf = {}
    for tier, views in hook_bins.items():
        if views:
            hook_perf[tier] = {"avg_views": round(sum(views) / len(views), 1), "count": len(views)}
    overrides["hook_performance"] = hook_perf

    # 4. Hashtag performance per pillar
    hashtag_views = defaultdict(lambda: defaultdict(list))
    for pub in publications:
        try:
            captions = json.loads(pub.get("captions_json") or "{}")
            platform_captions = captions.get(pub["platform"], {})
            for tag in platform_captions.get("hashtags", []):
                hashtag_views[pub["pillar"] or "ai_news"][tag].append(pub["views"])
        except (json.JSONDecodeError, TypeError):
            pass

    best_hashtags = {}
    for pillar, tags in hashtag_views.items():
        avg_by_tag = {t: sum(v) / len(v) for t, v in tags.items() if len(v) >= 3}
        if avg_by_tag:
            sorted_tags = sorted(avg_by_tag.items(), key=lambda x: x[1], reverse=True)[:5]
            best_hashtags[pillar] = [{"tag": t, "avg_views": round(v, 1)} for t, v in sorted_tags]
    overrides["best_hashtags_per_pillar"] = best_hashtags

    # Write overrides to DB
    now = datetime.now(timezone.utc).isoformat()
    with db._get_conn_ctx() as conn:
        for key, value in overrides.items():
            conn.execute("""
                INSERT INTO performance_overrides (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
            """, (key, json.dumps(value), now))

    logger.info(f"Feedback aggregation complete: {len(publications)} publications analyzed, "
                f"{len(overrides)} overrides written")
    return {"status": "ok", "publications_analyzed": len(publications), "overrides": overrides}


def get_override(key: str) -> dict:
    """Read a performance override from the DB.

    Args:
        key: Override key (e.g. "best_pillar_per_platform").

    Returns:
        Parsed JSON value, or empty dict if not found.
    """
    try:
        from database import get_db
        db = get_db()
        with db._get_conn_ctx() as conn:
            row = conn.execute(
                "SELECT value_json FROM performance_overrides WHERE key = ?", (key,)
            ).fetchone()
        if row:
            return json.loads(row["value_json"])
    except Exception:
        pass
    return {}

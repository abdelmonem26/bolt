"""
Bolt AI -- Caption Composer (caption_composer.py)
==================================================
Generates platform-specific metadata at DISTRIBUTION time, not script time.

Pre-plan Section 23: "Separate module called by each adapter. Takes script
+ article + platform + config. Returns platform-optimised metadata. Has access
to content_extractor output and historical performance data."

This is where Bolt's content-awareness advantage over Repurpose.io lives.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger("bolt.dist.captions")


def compose_caption(script: dict, article: dict, platform: str,
                    config: dict) -> dict:
    """
    Generate platform-specific title, caption, description, and hashtags.

    Args:
        script:   Script dict with 'script' text, 'pillar', 'content_id', 'captions'
        article:  Source article dict with 'title', 'source', 'link'
        platform: 'youtube' | 'tiktok' | 'instagram'
        config:   Full config dict

    Returns:
        Dict with platform-appropriate metadata keys.
    """
    script_text = script.get("script", "")
    pillar = script.get("pillar", "ai_news")
    title = article.get("title", "AI News Update")
    source = article.get("source", "")
    link = article.get("link", "")

    # Extract hook (first sentence) and punchline (last sentence before catchphrase)
    sentences = [s.strip() for s in re.split(r'[.!?]', script_text) if s.strip()]
    hook = sentences[0] if sentences else title[:80]
    punchline = sentences[-2] if len(sentences) >= 3 else sentences[-1] if sentences else ""

    # Get pillar-specific hashtags from config
    base_hashtags = list(config.get("hashtags", {}).get(pillar, ["#AI", "#AINews"]))

    # Add article-keyword hashtags
    keyword_hashtags = _extract_keyword_hashtags(title)
    all_hashtags = _deduplicate(base_hashtags + keyword_hashtags)

    # Load performance-based hashtag preferences if available
    perf_hashtags = _get_performance_hashtags(pillar, platform, config)
    if perf_hashtags:
        all_hashtags = _deduplicate(perf_hashtags + all_hashtags)

    # Select best affiliate link for this content (pre-plan Section 22)
    affiliate = _select_affiliate_link(pillar, title, config)

    if platform == "youtube":
        return _compose_youtube(hook, script_text, title, source, link, all_hashtags, config, affiliate)
    elif platform == "tiktok":
        return _compose_tiktok(hook, punchline, script_text, all_hashtags, config, affiliate)
    elif platform == "instagram":
        return _compose_instagram(punchline, script_text, all_hashtags, config, affiliate)
    else:
        return {"title": title[:100], "caption": script_text[:200], "hashtags": all_hashtags[:5]}


def _compose_youtube(hook: str, script_text: str, title: str,
                     source: str, link: str, hashtags: list, config: dict,
                     affiliate: dict = None) -> dict:
    """
    YouTube: title from hook (max 100 chars), description with source URL,
    up to 15 tags, category 28.
    """
    yt_title = f"{hook[:90]} #Shorts" if len(hook) <= 90 else f"{hook[:87]}... #Shorts"
    aff_line = ""
    if affiliate and affiliate.get("url"):
        aff_line = f"\n{affiliate['label']}: {affiliate['url']}\n"
    description = (
        f"{script_text[:200]}...\n\n"
        f"Source: {source}\n"
        f"{link}\n"
        f"{aff_line}\n"
        f"{' '.join(hashtags[:10])}\n\n"
        f"Subscribe for daily AI news!"
    )
    tags = [h.lstrip("#") for h in hashtags[:15]]
    return {
        "title": yt_title,
        "description": description[:5000],
        "hashtags": hashtags[:15],
        "tags": tags,
    }


def _compose_tiktok(hook: str, punchline: str, script_text: str,
                    hashtags: list, config: dict, affiliate: dict = None) -> dict:
    """
    TikTok: hook + 2 key facts + catchphrase + 3-5 hashtags.
    Pre-plan says NEVER more than 5 hashtags.
    """
    caption = f"{hook}"
    if punchline and punchline != hook:
        caption += f" {punchline}"
    caption += f" {' '.join(hashtags[:5])}"
    return {
        "caption": caption[:2200],
        "hashtags": hashtags[:5],
    }


def _compose_instagram(punchline: str, script_text: str,
                       hashtags: list, config: dict, affiliate: dict = None) -> dict:
    """
    Instagram: punchline as standalone caption, up to 30 hashtags
    posted as first comment (not in caption).
    """
    caption = punchline if punchline else script_text[:150]
    caption += "\n\nFollow @BoltAI for daily AI updates"
    return {
        "caption": caption[:2200],
        "hashtags": hashtags[:30],  # Posted as first comment per pre-plan
    }


# ── Hashtag helpers ────────────────────────────────────────────────────────

KEYWORD_MAP = {
    "openai": "#OpenAI", "gpt": "#GPT", "claude": "#Claude",
    "anthropic": "#Anthropic", "gemini": "#Gemini", "google": "#Google",
    "meta": "#Meta", "llama": "#LLaMA", "chatgpt": "#ChatGPT",
    "open source": "#OpenSource", "free": "#FreeAI", "robot": "#Robotics",
    "nvidia": "#NVIDIA", "microsoft": "#Microsoft", "apple": "#Apple",
    "agent": "#AIAgents", "local": "#LocalAI",
}


def _extract_keyword_hashtags(title: str) -> list:
    """Extract relevant hashtags from the article title."""
    title_lower = title.lower()
    return [tag for kw, tag in KEYWORD_MAP.items() if kw in title_lower][:5]


def _deduplicate(items: list) -> list:
    """Preserve order, remove duplicates (case-insensitive)."""
    seen = set()
    result = []
    for item in items:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _select_affiliate_link(pillar: str, article_title: str, config: dict) -> dict:
    """Select the best affiliate link for this content based on pillar and keywords.

    Pre-plan Section 22: "Caption composer selects the highest-earning affiliate
    link relevant to the specific video topic, not a generic link."

    Returns:
        Dict with 'url' and 'label', or empty dict if none configured.
    """
    links_config = config.get("affiliate_links", {})
    pillar_links = links_config.get(pillar, [])
    if not pillar_links:
        default = links_config.get("default", {})
        return default if isinstance(default, dict) else {}

    if isinstance(pillar_links, dict):
        return pillar_links  # Single link, not a list

    # Score each link by keyword overlap with article title
    title_lower = article_title.lower()
    best_link = pillar_links[0]  # Default to first
    best_score = 0

    for link in pillar_links:
        if not link.get("url"):
            continue
        keywords = link.get("keywords", [])
        score = sum(1 for kw in keywords if kw.lower() in title_lower)
        if score > best_score:
            best_score = score
            best_link = link

    return {"url": best_link.get("url", ""), "label": best_link.get("label", "")}


def _get_performance_hashtags(pillar: str, platform: str,
                              config: dict) -> list:
    """
    Load learned hashtag preferences from the feedback aggregator.
    Returns best-performing hashtags for this pillar+platform combination.

    Pre-plan Section 22: "Content-aware hashtag selection. Hashtags that
    appeared in high-performing posts of the same pillar are weighted higher."
    """
    try:
        from database import get_db
        db = get_db()
        # Query publications with highest engagement for this pillar
        # This will be populated by the feedback_aggregator once enough data exists
        with db._get_conn_ctx() as conn:
            rows = conn.execute("""
                SELECT p.content_id, p.views, p.engagement_rate
                FROM publications p
                JOIN scripts s ON p.content_id = s.content_id
                WHERE s.pillar = ? AND p.platform = ? AND p.views > 0
                ORDER BY p.engagement_rate DESC LIMIT 5
            """, (pillar, platform)).fetchall()
        # For now return empty -- feedback_aggregator will populate this
        return []
    except Exception:
        return []

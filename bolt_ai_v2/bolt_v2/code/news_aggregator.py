#!/usr/bin/env python3
"""
Bolt AI — News Aggregator v2
Fetches, filters, scores and ranks AI news using Claude for intelligent analysis.
Runs every 6 hours and writes top stories to the content queue.
"""

import asyncio
import aiohttp
import feedparser
import json
import logging
import re
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
import anthropic

from http_utils import async_cached_get, cached_get, HTTPError

logger = logging.getLogger("bolt.aggregator")


def load_config(path: str = "code/config.json") -> dict:
    """Legacy loader -- prefer shared_config.get_config() for secret injection."""
    from shared_config import get_config
    return get_config(path)


def clean_html(raw: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    clean = re.sub(r"<[^>]+>", " ", raw or "")
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip()


def article_age_hours(published_parsed) -> float:
    """Return article age in hours, or 999 if unknown."""
    if not published_parsed:
        return 999
    try:
        pub = datetime(*published_parsed[:6], tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - pub).total_seconds() / 3600
        return max(0, age)
    except Exception:
        return 999


def timeliness_score(age_hours: float) -> float:
    """Score from 1.0 (brand new) down to 0.0 (>72 h old)."""
    if age_hours <= 6:
        return 1.0
    if age_hours <= 24:
        return 0.8
    if age_hours <= 48:
        return 0.5
    if age_hours <= 72:
        return 0.2
    return 0.0


def deduplicate(articles: list[dict], seen_hashes: set) -> list[dict]:
    """Remove articles with identical or near-identical titles."""
    unique = []
    for a in articles:
        h = hashlib.md5(a["title"].lower().strip().encode()).hexdigest()
        if h not in seen_hashes:
            seen_hashes.add(h)
            unique.append(a)
    return unique


async def fetch_feed(session: aiohttp.ClientSession, name: str, info: dict) -> list[dict]:
    """Fetch a single RSS feed with caching and return parsed articles.
    
    Uses http_utils.async_cached_get for disk caching (avoids re-fetching
    the same RSS content within the cache TTL) and per-host rate limiting.
    """
    articles = []
    try:
        text = await async_cached_get(session, info["url"], cache_ttl_hours=1.0, timeout_s=10)
        if not text:
            return articles
        feed = feedparser.parse(text)
        for entry in feed.entries:
            title = getattr(entry, "title", "").strip()
            summary = clean_html(getattr(entry, "summary", title))
            link = getattr(entry, "link", "")
            published = getattr(entry, "published_parsed", None)
            if not title:
                continue
            age = article_age_hours(published)
            articles.append({
                "title": title,
                "summary": summary[:500],
                "link": link,
                "source": name,
                "reliability": info.get("reliability", 0.7),
                "age_hours": age,
                "timeliness": timeliness_score(age),
                "published_iso": datetime.now(timezone.utc).isoformat() if age == 999 else (
                    datetime(*published[:6], tzinfo=timezone.utc).isoformat()
                ),
            })
    except Exception as e:
        logger.warning(f"Feed error [{name}]: {e}")
    return articles


async def fetch_all_feeds(config: dict) -> list[dict]:
    """Concurrently fetch all configured RSS feeds."""
    sources = config.get("news_sources", {})
    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [fetch_feed(session, name, info) for name, info in sources.items()]
        results = await asyncio.gather(*tasks)
    all_articles = [a for batch in results for a in batch]
    logger.info(f"Fetched {len(all_articles)} raw articles from {len(sources)} sources")
    return all_articles


def pre_filter(articles: list[dict]) -> list[dict]:
    """
    Keep only articles that are clearly AI-related and recent (<72 h).
    Uses a keyword set — no API call needed at this stage.
    """
    MUST_HAVE = {
        "artificial intelligence", "machine learning", "deep learning",
        "neural network", "large language model", "llm", "gpt", "gemini",
        "claude", "chatgpt", "openai", "anthropic", "deepmind", "hugging face",
        "ai model", "ai tool", "ai startup", "ai regulation", "ai chip",
        "diffusion model", "transformer", "inference", "fine-tun", "rlhf",
        "generative ai", "gen ai", "multimodal", "robotics", "automation",
        "nvidia", "stable diffusion", "midjourney", "sora",
    }
    filtered = []
    for a in articles:
        if a["age_hours"] > 72:
            continue
        combined = (a["title"] + " " + a["summary"]).lower()
        if any(kw in combined for kw in MUST_HAVE):
            filtered.append(a)
    logger.info(f"Pre-filter: {len(filtered)} articles remain")
    return filtered


def score_article_heuristic(article: dict) -> float:
    """
    Fast heuristic score (0–10) before calling Claude.
    Combines reliability, timeliness, and title impact.
    """
    base = (
        article["reliability"] * 4.0
        + article["timeliness"] * 3.0
    )
    # Boost for high-impact keywords in title
    IMPACT_WORDS = {"breakthrough", "launches", "announces", "released", "ban",
                    "beats", "surpasses", "first", "record", "billion", "trillion",
                    "open-source", "free", "new model", "gpt-5", "claude 4", "gemini"}
    title_lower = article["title"].lower()
    boosts = sum(1 for w in IMPACT_WORDS if w in title_lower)
    base += min(boosts * 0.5, 2.0)
    return min(base, 9.0)


def claude_batch_score(articles: list[dict], config: dict) -> list[dict]:
    """
    Ask Claude to score and rank the top candidates.
    Returns articles with added 'claude_score' and 'content_pillar' fields.
    """
    api_key = config["apis"].get("anthropic_api_key", "")
    if not api_key or api_key.startswith("YOUR_"):
        logger.warning("No Anthropic API key — skipping Claude scoring")
        for a in articles:
            a["claude_score"] = a.get("heuristic_score", 5.0)
            a["pillar"] = "ai_news"
            a["content_pillar"] = "ai_news"
            a["claude_hook_idea"] = ""
        return articles

    client = anthropic.Anthropic(api_key=api_key)

    articles_text = "\n\n".join(
        f"[{i+1}] SOURCE: {a['source']}\nTITLE: {a['title']}\nSUMMARY: {a['summary']}"
        for i, a in enumerate(articles)
    )

    prompt = f"""You are the editorial AI for Bolt, an AI robot news creator targeting US tech-curious audiences on YouTube Shorts, TikTok, and Instagram Reels.

Score each article below for SHORT-FORM VIDEO POTENTIAL on a scale of 0–10.
A perfect 10 means: surprising, timely, simple to explain in 45 seconds, has emotional hook.
Score low if: too technical, niche research paper, no clear human impact, already widely covered.

Also classify each into one pillar: ai_news | ai_tools | ai_concepts | ai_daily_life
Also suggest a 1-sentence video hook for the top 3 articles.

Articles:
{articles_text}

Respond ONLY with valid JSON in this exact format:
{{
  "scores": [
    {{"rank": 1, "score": 9.2, "pillar": "ai_news", "hook": "Hook sentence here"}},
    {{"rank": 2, "score": 8.1, "pillar": "ai_tools", "hook": ""}},
    ...
  ]
}}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text
        raw = re.sub(r"```json|```", "", raw).strip()
        data = json.loads(raw)
        scores = data.get("scores", [])
        for i, a in enumerate(articles):
            if i < len(scores):
                a["claude_score"] = scores[i]["score"]
                a["pillar"] = scores[i].get("pillar", "ai_news")
                a["content_pillar"] = a["pillar"]  # backward compat
                a["claude_hook_idea"] = scores[i].get("hook", "")
            else:
                a["claude_score"] = a.get("heuristic_score", 5.0)
                a["pillar"] = "ai_news"
                a["content_pillar"] = "ai_news"
                a["claude_hook_idea"] = ""
    except Exception as e:
        logger.error(f"Claude scoring failed: {e}. Using heuristic scores.")
        for a in articles:
            a["claude_score"] = a.get("heuristic_score", 5.0)
            a["pillar"] = "ai_news"
            a["content_pillar"] = "ai_news"
            a["claude_hook_idea"] = ""
    return articles


def write_queue(articles: list[dict], config: dict) -> None:
    """Write top articles to the content queue as individual JSON files.

    DEPRECATED: Articles are now saved to the SQLite DB by the orchestrator
    (database.save_articles()). This function writes redundant JSON files for
    backward compatibility with script_generator's queue reader. Once
    script_generator reads exclusively from the DB, this function can be removed.
    """
    queue_dir = Path(config.get("paths", {}).get("queue", "data/queue"))
    queue_dir.mkdir(parents=True, exist_ok=True)

    # Clear old pending files
    for f in queue_dir.glob("pending_*.json"):
        f.unlink()

    for i, article in enumerate(articles[:5]):
        entry = {
            **article,
            "status": "pending_script",
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "queue_position": i + 1,
        }
        out_path = queue_dir / f"pending_{i+1:02d}_{article['source'][:20].replace(' ', '_')}.json"
        out_path.write_text(json.dumps(entry, indent=2, ensure_ascii=False))
        logger.info(f"Queued [{i+1}] {article['title'][:60]}... (score: {article['claude_score']:.1f})")


async def run(config: "dict | None" = None, *, config_path: str = "code/config.json") -> list[dict]:
    """Full aggregation pipeline. Returns top articles.

    Args:
        config: Pre-loaded config dict (preferred). When provided, config_path is ignored.
        config_path: Legacy fallback -- used only when config is None (CLI usage).
    """
    if config is None:
        config = load_config(config_path)

    # 1. Fetch all feeds concurrently
    raw = await fetch_all_feeds(config)

    # 2. Deduplicate -- use persistent hashes from the DB so the same story
    #    is never processed twice across pipeline runs (pre-plan rule).
    try:
        from database import get_db
        db = get_db()
        seen: set = db.get_seen_hashes()
        logger.info(f"Loaded {len(seen)} persistent article hashes from DB")
    except Exception as e:
        logger.warning(f"Could not load persistent hashes ({e}), using in-memory set")
        seen = set()

    raw = deduplicate(raw, seen)

    # Persist new hashes so subsequent runs skip these articles
    try:
        db.store_article_hashes(raw)
        db.prune_old_hashes(max_age_days=30)
    except Exception:
        pass  # non-critical -- dedup still works in-memory for this run

    # 3. Pre-filter by AI relevance + recency
    filtered = pre_filter(raw)
    if not filtered:
        logger.warning("No AI articles found — falling back to backup topics")
        filtered = get_backup_articles()

    # 4. Heuristic scoring
    for a in filtered:
        a["heuristic_score"] = score_article_heuristic(a)

    # Sort by heuristic, take top 15 for Claude
    candidates = sorted(filtered, key=lambda x: x["heuristic_score"], reverse=True)[:15]

    # 5. Claude AI scoring
    scored = claude_batch_score(candidates, config)

    # 6. Final sort by Claude score
    top = sorted(scored, key=lambda x: x["claude_score"], reverse=True)[:5]

    # 7. Write to queue
    write_queue(top, config)

    logger.info(f"Aggregation complete. Top story: {top[0]['title'][:70]}")
    return top


def get_backup_articles() -> list[dict]:
    """Evergreen backup articles for slow news days."""
    now = datetime.now(timezone.utc).isoformat()
    return [
        {
            "title": "Top 5 Free AI Tools That Will Change How You Work in 2026",
            "summary": "Five powerful free AI tools that are transforming daily productivity for millions of professionals.",
            "link": "", "source": "Bolt Backup", "reliability": 0.9,
            "age_hours": 0, "timeliness": 1.0, "published_iso": now,
        },
        {
            "title": "What Is an LLM and Why Should You Care?",
            "summary": "Large language models explained in simple terms — and why they matter for everyone, not just developers.",
            "link": "", "source": "Bolt Backup", "reliability": 0.9,
            "age_hours": 0, "timeliness": 1.0, "published_iso": now,
        },
        {
            "title": "AI vs Human Creativity: Who's Actually Winning?",
            "summary": "A look at where AI genuinely beats humans, where it still falls short, and what that means for creative professions.",
            "link": "", "source": "Bolt Backup", "reliability": 0.9,
            "age_hours": 0, "timeliness": 1.0, "published_iso": now,
        },
    ]


def probe_feeds(config_path: str = "code/config.json") -> list[dict]:
    """Probe all configured RSS feeds using http_utils.cached_get (synchronous).

    Tests each feed URL for reachability, valid RSS content, and article count.
    Uses the cached_get retry/rate-limit machinery so probes are polite and
    cached for 1 hour (subsequent probes within that window hit disk).

    Returns a list of probe results, one per feed:
        {
            "name": str,
            "url": str,
            "status": "ok" | "error" | "empty",
            "article_count": int,
            "error": str | None,
            "cached": bool,
        }
    """
    config = load_config(config_path)
    sources = config.get("news_sources", {})
    results = []

    for name, info in sources.items():
        url = info.get("url", "")
        probe = {"name": name, "url": url, "status": "error",
                 "article_count": 0, "error": None, "cached": False}
        try:
            text = cached_get(url, cache_ttl_hours=1.0, max_retries=2, timeout=10)
            if not text:
                probe["status"] = "empty"
                probe["error"] = "Empty response"
                results.append(probe)
                continue

            # If we got a string back (not JSON), parse as RSS
            raw = text if isinstance(text, str) else json.dumps(text)
            feed = feedparser.parse(raw)
            n_entries = len(feed.entries)
            probe["article_count"] = n_entries

            if n_entries == 0:
                probe["status"] = "empty"
                probe["error"] = "Feed parsed but contained 0 entries"
            else:
                probe["status"] = "ok"

            logger.info("Probe [%s]: %s (%d entries)", name, probe["status"], n_entries)

        except HTTPError as e:
            probe["error"] = f"HTTP {e.status_code}: {e.detail[:120]}"
            logger.warning("Probe [%s] failed: %s", name, probe["error"])
        except Exception as e:
            probe["error"] = str(e)[:200]
            logger.warning("Probe [%s] failed: %s", name, probe["error"])

        results.append(probe)

    ok = sum(1 for r in results if r["status"] == "ok")
    logger.info("Feed probe complete: %d/%d sources reachable", ok, len(results))
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "--probe":
        results = probe_feeds()
        print(f"\n{'='*60}")
        print(f"  RSS Feed Probe Results ({sum(1 for r in results if r['status']=='ok')}/{len(results)} OK)")
        print(f"{'='*60}")
        for r in results:
            icon = "OK" if r["status"] == "ok" else "EMPTY" if r["status"] == "empty" else "FAIL"
            print(f"  [{icon:5s}] {r['name']:30s} {r['article_count']:3d} entries"
                  + (f"  -- {r['error']}" if r["error"] else ""))
        print(f"{'='*60}\n")
    else:
        top_stories = asyncio.run(run())
        print(f"\n Top {len(top_stories)} stories queued:\n")
        for i, s in enumerate(top_stories, 1):
            print(f"  {i}. [{s['claude_score']:.1f}] {s['title']}")


# ════════════════════════════════════════════════════════════════════
# RESTORED FUNCTIONS — migrated from original NewsAggregator class
# ════════════════════════════════════════════════════════════════════

def identify_trending_topics(articles: list[dict], top_n: int = 5,
                               num_clusters: int = 5) -> tuple[list, list]:
    """
    Identify trending topics using TF-IDF and KMeans clustering.
    Replaces NewsAggregator.identify_trending_topics() from v1.
    Returns (trending_clusters, top_keywords).
    """
    if not articles:
        return [], []
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.cluster import KMeans
        import nltk
        from nltk.tokenize import word_tokenize
        from nltk.corpus import stopwords
        from collections import Counter

        stop_words = set(stopwords.words("english"))
        summaries  = [a.get("summary", a.get("title", "")) for a in articles]

        # TF-IDF clustering
        vectorizer = TfidfVectorizer(stop_words="english", max_features=500)
        matrix     = vectorizer.fit_transform(summaries)
        k          = min(num_clusters, len(articles))
        kmeans     = KMeans(n_clusters=k, random_state=42, n_init=10)
        kmeans.fit(matrix)

        centroids = kmeans.cluster_centers_.argsort()[:, ::-1]
        terms     = vectorizer.get_feature_names_out()
        trending  = [[terms[i] for i in centroids[c, :top_n]] for c in range(k)]

        # Top keywords overall
        all_text = " ".join(summaries)
        tokens   = [w for w in word_tokenize(all_text.lower()) if w.isalpha() and w not in stop_words]
        top_kw   = [w for w, _ in Counter(tokens).most_common(top_n)]

        logger.info(f"Trending topics identified: {len(trending)} clusters, {len(top_kw)} keywords")
        return trending, top_kw

    except ImportError:
        logger.debug("sklearn/nltk not available for topic clustering")
        return [], []
    except Exception as e:
        logger.warning(f"identify_trending_topics failed: {e}")
        return [], []


def flesch_reading_ease(text: str) -> float:
    """
    Calculate Flesch Reading Ease score for a script.
    Replaces NewsAggregator.flesch_reading_ease() from v1.
    Higher = easier to read. Bolt targets 60–70 (standard/plain English).
    """
    if not text:
        return 0.0
    try:
        import nltk
        sentences = nltk.sent_tokenize(text)
        words     = nltk.word_tokenize(text)
        if not sentences or not words:
            return 0.0
        n_sent  = len(sentences)
        n_words = len(words)
        n_syl   = count_syllables(words)
        if n_sent == 0 or n_words == 0:
            return 0.0
        score = 206.835 - 1.015 * (n_words / n_sent) - 84.6 * (n_syl / n_words)
        return round(max(0.0, min(100.0, score)), 1)
    except Exception:
        # Rough heuristic fallback
        words = text.split()
        sentences = text.split(".")
        if not sentences:
            return 50.0
        avg_words = len(words) / max(len(sentences), 1)
        return max(0, min(100, 100 - avg_words * 3))


def count_syllables(words: list[str]) -> int:
    """
    Simple syllable counter.
    Replaces NewsAggregator.count_syllables() from v1.
    """
    import re
    count = 0
    for word in words:
        word  = word.lower()
        count += max(1, len(re.findall(r"[aeiouy]+", word)))
    return count


def is_us_relevant(title: str, summary: str, config: dict) -> bool:
    """
    Check if an article is relevant to a US audience.
    Replaces NewsAggregator.is_us_relevant() from v1.
    Note: In the current version geo-filtering is intentionally relaxed —
    AI news is global and the US audience is the TARGET, not a geo-filter.
    This function is preserved for backward compatibility and optional use.
    """
    # AI topics are globally relevant to US tech audiences
    # Only filter OUT content that is explicitly non-US AND non-AI
    ai_keywords = {"ai", "artificial intelligence", "machine learning", "llm",
                   "openai", "anthropic", "google", "meta", "microsoft"}
    combined = (title + " " + summary).lower()
    return any(kw in combined for kw in ai_keywords)

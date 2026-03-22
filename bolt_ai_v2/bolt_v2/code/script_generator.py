#!/usr/bin/env python3
"""
Bolt AI — Script Generator v2
Uses Claude to write punchy, platform-optimised 45-second scripts in Bolt's voice.
Reads from the content queue, writes completed scripts back with quality scores.
"""

import json
import logging
import re
import random
from datetime import datetime, timezone
from pathlib import Path
import anthropic

logger = logging.getLogger("bolt.scriptgen")


def load_config(path: str = "code/config.json") -> dict:
    """Legacy loader -- prefer shared_config.get_config() for secret injection."""
    from shared_config import get_config
    return get_config(path)


BOLT_SYSTEM_PROMPT = """You are BOLT — an enthusiastic AI robot news reporter with a fun, slightly robotic personality.
You deliver AI news for YouTube Shorts, TikTok, and Instagram Reels.

Your voice rules:
- Hook the viewer in the FIRST 3 SECONDS with something surprising, shocking or intriguing
- Use simple language — a 14-year-old should understand everything
- Never say "in conclusion" or "to sum up"
- Occasionally use light robot humor ("my circuits are buzzing", "beep boop — this is wild")
- Always end with ONE of Bolt's catchphrases
- Scripts must be 100–130 words (fits exactly in 45 seconds at Bolt's speaking pace)
- Write for ears, not eyes — short punchy sentences, no lists

Bolt's catchphrases (rotate, never repeat the same one twice in a row):
- "Stay curious, humans!"
- "Let's get wired!"
- "Bolt out — keep charging!"
- "Until next time, stay plugged in!"
- "That's your AI download for today — Bolt out!"
"""

SCRIPT_FORMAT = """
[HOOK 0-3s] — One sentence. No greeting. No "hey guys." Stop the scroll with something alarming, counterintuitive, or specific beyond expectation. The first word must be signal, not noise.
[STAKES 3-8s] — Why this matters to the VIEWER. Anchor the story to their life. Widen the gap between the hook's question and its answer. Do NOT answer the hook yet.
[PAYLOAD 8-30s] — Maximum 3 facts. Each simpler than the last so momentum builds. Explain jargon by analogy, not definition. Every sentence adds new information or intensifies the previous one. Never recap.
[PUNCHLINE 30-40s] — Answer the hook's question with something the viewer did NOT predict. The ending must reframe the story. This is why people share.
[CTA + CATCHPHRASE 40-45s] — Exactly ONE ask (follow OR link, never both), then Bolt's closing catchphrase.
"""

PLATFORM_CAPTIONS_PROMPT = """Based on this Bolt video script, write optimised captions for 3 platforms.

Script:
{script}

Topic: {title}
Content Pillar: {pillar}
Hashtags available: {hashtags}

Return ONLY valid JSON:
{{
  "youtube": {{
    "title": "SEO title under 80 chars, includes main keyword",
    "description": "2-3 sentence description with keywords, ends with subscribe CTA. Under 200 chars.",
    "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
    "hashtags": ["#Tag1", "#Tag2", "#Tag3"]
  }},
  "tiktok": {{
    "caption": "Casual punchy caption under 150 chars with 5 trending hashtags",
    "hashtags": ["#Tag1", "#Tag2", "#Tag3", "#Tag4", "#Tag5"]
  }},
  "instagram": {{
    "caption": "Engaging caption 100–150 chars with CTA and 8-10 hashtags",
    "hashtags": ["#Tag1", "#Tag2", "#Tag3", "#Tag4", "#Tag5", "#Tag6", "#Tag7", "#Tag8"]
  }}
}}"""

QUALITY_SCORING_PROMPT = """Score this Bolt AI script on these criteria. Return ONLY valid JSON.

Script:
{script}

{{
  "hook_strength": <0-10, does it grab attention in first 3 seconds?>,
  "simplicity": <0-10, is the language clear for a general audience?>,
  "bolt_voice": <0-10, does it sound like Bolt's fun robot personality?>,
  "word_count": <actual word count>,
  "pacing": <0-10, short punchy sentences, easy to deliver in 45s?>,
  "overall_score": <0-10, weighted average>,
  "pass": <true if overall >= 8.5>,
  "feedback": "One sentence of specific improvement if score < 8.5, else empty string"
}}"""


def get_todays_pillar(config: dict) -> str:
    day = datetime.now().strftime("%A").lower()
    return config["content_pillars"].get(day, "ai_news")


def load_queue_item(config: dict) -> dict | None:
    """Load the highest-priority pending article from the queue."""
    queue_dir = Path(config["paths"]["queue"])
    pending = sorted(queue_dir.glob("pending_*.json"))
    if not pending:
        return None
    return json.loads(pending[0].read_text())


def generate_script(article: dict, pillar: str, config: dict, attempt: int = 1) -> dict:
    """Generate a Bolt script via Claude. Returns dict with script + metadata."""
    api_key = config["apis"].get("anthropic_api_key", "")
    if not api_key or api_key.startswith("YOUR_"):
        return _fallback_script(article, pillar, config)

    client = anthropic.Anthropic(api_key=api_key)
    catchphrase = random.choice(config["character"]["catchphrases"])
    hook_idea = article.get("claude_hook_idea", "")
    hook_note = f"\nSuggested hook angle: {hook_idea}" if hook_idea else ""

    user_prompt = f"""Write a Bolt script for this AI news story.

Title: {article['title']}
Summary: {article['summary']}
Content Pillar: {pillar}
Catchphrase to use: "{catchphrase}"{hook_note}

Follow this structure:
{SCRIPT_FORMAT}

Rules:
- 100–130 words total (STRICT — count them)
- Do NOT use markdown, bullet points or section labels in output
- The script should flow as one continuous spoken piece
- Make it sound EXCITING even if the topic is technical
{"- This is attempt " + str(attempt) + ". Previous version was too " + ("long" if attempt == 2 else "generic") + " — fix that." if attempt > 1 else ""}

Return ONLY the script text. Nothing else."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        system=BOLT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}]
    )
    script = response.content[0].text.strip()
    return {"script": script, "pillar": pillar, "catchphrase": catchphrase}


def score_script(script: str, config: dict) -> dict:
    """Ask Claude to quality-score the script. Falls back to heuristic."""
    api_key = config["apis"].get("anthropic_api_key", "")
    if not api_key or api_key.startswith("YOUR_"):
        return _heuristic_score(script)

    client = anthropic.Anthropic(api_key=api_key)
    prompt = QUALITY_SCORING_PROMPT.format(script=script)
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = re.sub(r"```json|```", "", resp.content[0].text).strip()
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"Score parsing failed: {e}")
        return _heuristic_score(script)


def generate_platform_captions(script: str, article: dict, pillar: str, config: dict) -> dict:
    """Generate platform-specific captions via Claude."""
    api_key = config["apis"].get("anthropic_api_key", "")
    if not api_key or api_key.startswith("YOUR_"):
        return _fallback_captions(article, pillar, config)

    client = anthropic.Anthropic(api_key=api_key)
    hashtags = config["hashtags"].get(pillar, config["hashtags"]["ai_news"])
    prompt = PLATFORM_CAPTIONS_PROMPT.format(
        script=script,
        title=article["title"],
        pillar=pillar,
        hashtags=", ".join(hashtags),
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = re.sub(r"```json|```", "", resp.content[0].text).strip()
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"Caption generation failed: {e}")
        return _fallback_captions(article, pillar, config)


def run_with_retry(article: dict, pillar: str, config: dict, max_attempts: int = 3) -> dict | None:
    """Generate script with auto-retry if quality gate fails."""
    for attempt in range(1, max_attempts + 1):
        logger.info(f"Script attempt {attempt}/{max_attempts} for: {article['title'][:50]}...")
        result = generate_script(article, pillar, config, attempt)
        score = score_script(result["script"], config)
        result["quality"] = score

        word_count = score.get("word_count", len(result["script"].split()))
        overall = score.get("overall_score", 5.0)

        logger.info(f"  Score: {overall:.1f}/10 | Words: {word_count} | Pass: {score.get('pass', False)}")

        if score.get("pass") and config["quality_gate"]["min_script_words"] <= word_count <= config["quality_gate"]["max_script_words"]:
            logger.info(f"✅ Script passed quality gate on attempt {attempt}")
            return result

        if attempt < max_attempts:
            feedback = score.get("feedback", "")
            logger.info(f"  Retrying... Feedback: {feedback}")

    logger.warning("⚠️  Script did not pass after max retries — queuing for human review")
    return result  # Return best effort for human review


def save_script_to_queue(article: dict, script_result: dict, captions: dict, config: dict) -> Path:
    # Run content validator before saving
    try:
        from content_validator import ContentValidator
        validator = ContentValidator(config)
        v_result = validator.validate_and_log(
            script=script_result["script"],
            article=article,
            content_id="pre-save",
        )
        if not v_result.passed:
            logger.warning(f"Content validator failures: {v_result.failures}")
            # Downgrade score so it goes to human review
            if script_result.get("quality"):
                script_result["quality"]["validator_failures"] = v_result.failures
                script_result["quality"]["overall_score"] = min(
                    script_result["quality"]["overall_score"],
                    config.get("automation",{}).get("auto_publish_threshold",8.5) - 0.1
                )
    except ImportError:
        logger.debug("Content validator not available -- skipping validation")
    except Exception as e:
        logger.warning(f"Content validation error (non-fatal): {e}")

    """Write fully generated content package to the queue."""
    queue_dir = Path(config["paths"]["queue"])
    content_id = f"bolt_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    score = script_result["quality"].get("overall_score", 0)

    # Auto-approve logic:
    # 1. If score >= quality_gate.auto_approve_above (default 9.0), auto-approve regardless
    # 2. If auto_publish_enabled AND score >= auto_publish_threshold (8.5), auto-approve
    # 3. Otherwise, queue for human review
    qg_auto_approve = config.get("quality_gate", {}).get("auto_approve_above", 9.0)
    threshold = config["automation"]["auto_publish_threshold"]
    auto_approve = (
        score >= qg_auto_approve
        or (config["automation"]["auto_publish_enabled"] and score >= threshold)
    )

    package = {
        "content_id": content_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "approved" if auto_approve else "pending_review",
        "auto_approved": auto_approve,
        "article": article,
        "pillar": script_result["pillar"],
        "script": script_result["script"],
        "quality": script_result["quality"],
        "captions": captions,
    }

    out_path = queue_dir / f"script_{content_id}.json"
    out_path.write_text(json.dumps(package, indent=2, ensure_ascii=False))

    # Remove the raw article file
    for f in queue_dir.glob("pending_01_*.json"):
        f.unlink()

    status = "AUTO-APPROVED ✅" if auto_approve else "PENDING REVIEW 👁️"
    logger.info(f"Script saved [{status}]: {out_path.name}")
    return out_path


def _heuristic_score(script: str) -> dict:
    words = len(script.split())
    overall = 7.5 if 90 <= words <= 140 else 5.0
    return {
        "hook_strength": 7.0, "simplicity": 7.0, "bolt_voice": 7.0,
        "word_count": words, "pacing": 7.0, "overall_score": overall,
        "pass": overall >= 8.5, "feedback": "Manual review recommended without Claude API"
    }


def _fallback_script(article: dict, pillar: str, config: dict) -> dict:
    catchphrase = random.choice(config["character"]["catchphrases"])
    title = article["title"]
    summary = article["summary"][:200]
    script = (
        f"Hey tech humans, Bolt here! {title}. "
        f"{summary}. "
        f"This could change everything about how we use AI — and you need to know about it. "
        f"Follow Bolt for your daily AI download! {catchphrase}"
    )
    return {"script": script, "pillar": pillar, "catchphrase": catchphrase}


def _fallback_captions(article: dict, pillar: str, config: dict) -> dict:
    hashtags = config["hashtags"].get(pillar, ["#AI", "#TechNews"])
    return {
        "youtube": {
            "title": article["title"][:80],
            "description": f"AI update from Bolt: {article['summary'][:150]}. Subscribe for daily AI news!",
            "tags": ["AI", "artificial intelligence", "tech news", "machine learning"],
            "hashtags": hashtags[:3],
        },
        "tiktok": {
            "caption": f"⚡ {article['title'][:100]} | Follow @BoltAI for more!",
            "hashtags": hashtags[:5],
        },
        "instagram": {
            "caption": f"⚡ {article['title'][:120]} | Follow for daily AI updates! 🤖",
            "hashtags": hashtags[:8],
        },
    }


def run(config: dict | None = None, *, config_path: str = "code/config.json") -> dict | None:
    """Generate a script for the top queued article.

    Args:
        config: Pre-loaded config dict (preferred). When provided, config_path is ignored.
        config_path: Legacy fallback -- used only when config is None (CLI usage).
    """
    if config is None:
        config = load_config(config_path)
    pillar = get_todays_pillar(config)
    logger.info(f"Today's content pillar: {pillar}")

    article = load_queue_item(config)
    if not article:
        logger.warning("No articles in queue. Run news_aggregator.py first.")
        return None

    logger.info(f"Generating script for: {article['title'][:60]}...")
    script_result = run_with_retry(article, pillar, config)

    logger.info("Generating platform captions...")
    captions = generate_platform_captions(script_result["script"], article, pillar, config)

    out_path = save_script_to_queue(article, script_result, captions, config)
    return json.loads(out_path.read_text())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run()
    if result:
        print(f"\n📝 Script generated (score: {result['quality']['overall_score']:.1f}/10):\n")
        print(result["script"])
        print(f"\nStatus: {result['status']}")

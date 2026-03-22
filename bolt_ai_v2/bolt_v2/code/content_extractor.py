#!/usr/bin/env python3
"""Bolt AI -- Content Extractor (adapted from Operator 1 patterns)

Adapts two patterns from oso4242424242/githubu-isu-meanu:

1. **LLM Filing Extractor** (llm_filing_extractor.py) -> LLM Article Extractor
   - Uses Claude to extract structured insights from raw article text
   - Per-pillar taxonomy hints (like per-market taxonomy hints)
   - Validation via consistency checks (like accounting identity checks)
   - Structured extraction prompt with JSON output

2. **Fuzzy PDF Parser** (fuzzy_pdf_parser.py) -> Fuzzy Content Classifier
   - Uses fuzzy string matching to classify articles WITHOUT LLM (free)
   - Canonical concept dictionaries (like _INCOME_CONCEPTS/_BALANCE_CONCEPTS)
   - SequenceMatcher/rapidfuzz scoring (like _fuzzy_match_concept)
   - Falls back gracefully when no LLM key is available

Tiered architecture (like HKEX akshare -> scraper):
    Tier 1: Fuzzy classifier (FREE, instant, no API key)
    Tier 2: Claude LLM extractor (paid, deeper analysis)

Usage:
    from content_extractor import extract_article, fuzzy_classify

    # Free tier: fuzzy classification only
    classification = fuzzy_classify(title, summary)

    # Full tier: LLM deep extraction (falls back to fuzzy if no key)
    result = extract_article(title, summary, config)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Optional

logger = logging.getLogger("bolt.extractor")


# ---------------------------------------------------------------------------
# Extraction result dataclass (like ExtractionResult in llm_filing_extractor)
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    """Structured article extraction result."""
    title: str = ""
    pillar: str = "ai_news"
    category: str = ""
    impact_score: float = 0.0        # 0-10, like Claude editorial score
    sentiment: str = "neutral"       # positive | neutral | negative
    key_facts: list[str] = field(default_factory=list)
    hook_idea: str = ""              # 1-sentence video hook
    companies_mentioned: list[str] = field(default_factory=list)
    technologies_mentioned: list[str] = field(default_factory=list)
    audience_relevance: float = 0.0  # 0-10 for US tech audience
    source_method: str = ""          # fuzzy | llm_claude
    success: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Per-pillar taxonomy hints (adapted from _MARKET_TAXONOMY_HINTS)
#
# Like llm_filing_extractor's per-market hints that tell the LLM what
# regional accounting terms to look for, these tell Claude what content
# patterns to look for in each content pillar.
# ---------------------------------------------------------------------------

_PILLAR_TAXONOMY_HINTS: dict[str, str] = {
    "ai_news": (
        "PILLAR: AI News. Focus: breaking announcements, product launches, funding rounds.\n"
        "Key patterns to identify:\n"
        "- New model releases (GPT-5, Claude 4, Gemini 2, etc.)\n"
        "- Company announcements (acquisitions, partnerships, funding)\n"
        "- Regulatory actions (bans, guidelines, executive orders)\n"
        "- Research breakthroughs (papers, benchmarks, capabilities)\n"
        "- Industry milestones (user counts, revenue figures, adoption rates)\n"
        "Hook style: urgent, breaking-news energy, 'just dropped' language.\n"
    ),
    "ai_tools": (
        "PILLAR: AI Tools. Focus: practical tools people can use today.\n"
        "Key patterns to identify:\n"
        "- Free tools and platforms (open-source, freemium)\n"
        "- Productivity improvements (before/after comparisons)\n"
        "- Step-by-step capabilities (what it can DO for you)\n"
        "- Pricing and accessibility (free tier, cost per use)\n"
        "- Integration with existing workflows (Slack, VS Code, etc.)\n"
        "Hook style: 'you can use this RIGHT NOW' energy.\n"
    ),
    "ai_concepts": (
        "PILLAR: AI Concepts. Focus: explaining AI ideas simply.\n"
        "Key patterns to identify:\n"
        "- Technical concepts made accessible (transformers, RLHF, etc.)\n"
        "- How things work under the hood (simplified)\n"
        "- Common misconceptions to correct\n"
        "- Analogies and metaphors for complex ideas\n"
        "- Historical context (how we got here)\n"
        "Hook style: 'ever wondered how X works?' curiosity-driven.\n"
    ),
    "ai_daily_life": (
        "PILLAR: AI in Daily Life. Focus: real-world AI impact.\n"
        "Key patterns to identify:\n"
        "- AI in healthcare, education, transportation, finance\n"
        "- Job market impact (new roles, automation risks)\n"
        "- Consumer products using AI (phones, cars, apps)\n"
        "- Ethical concerns and social implications\n"
        "- Personal stories and human-AI interaction\n"
        "Hook style: 'this affects YOU' personal relevance.\n"
    ),
}


# ---------------------------------------------------------------------------
# Canonical concept dictionaries for fuzzy matching
# (Adapted from fuzzy_pdf_parser's _INCOME_CONCEPTS / _BALANCE_CONCEPTS)
#
# Like the financial concept dictionaries that map row labels to canonical
# field names, these map article text patterns to content categories.
# ---------------------------------------------------------------------------

# (pattern, category) -- ordered most-specific first
_NEWS_CONCEPTS: list[tuple[str, str]] = [
    # Model releases
    ("new model release", "model_release"),
    ("launches new ai model", "model_release"),
    ("introduces new language model", "model_release"),
    ("open source model", "model_release"),
    ("released a new version", "model_release"),
    ("next generation model", "model_release"),
    # Funding / business
    ("raises funding", "funding"),
    ("series a", "funding"),
    ("series b", "funding"),
    ("series c", "funding"),
    ("billion dollar valuation", "funding"),
    ("acquisition", "acquisition"),
    ("acquires", "acquisition"),
    ("merger", "acquisition"),
    ("partnership", "partnership"),
    ("collaborat", "partnership"),
    # Regulation
    ("regulation", "regulation"),
    ("executive order", "regulation"),
    ("ban on ai", "regulation"),
    ("ai safety", "safety"),
    ("alignment", "safety"),
    ("ai governance", "regulation"),
    ("legislation", "regulation"),
    # Research
    ("research paper", "research"),
    ("breakthrough", "research"),
    ("state of the art", "research"),
    ("benchmark", "research"),
    ("outperforms", "research"),
    ("novel approach", "research"),
    # Products / tools
    ("free tool", "tool_launch"),
    ("free ai tool", "tool_launch"),
    ("open source", "tool_launch"),
    ("api launch", "tool_launch"),
    ("developer tool", "tool_launch"),
    ("plugin", "tool_launch"),
    ("integration", "tool_launch"),
    # Industry impact
    ("job market", "industry_impact"),
    ("replaces human", "industry_impact"),
    ("automat", "industry_impact"),
    ("workforce", "industry_impact"),
    ("layoff", "industry_impact"),
    ("productivity", "industry_impact"),
    # Hardware
    ("chip", "hardware"),
    ("gpu", "hardware"),
    ("semiconductor", "hardware"),
    ("data center", "hardware"),
    ("compute", "hardware"),
]

# Company name patterns for entity extraction
_COMPANY_PATTERNS: list[tuple[str, str]] = [
    ("openai", "OpenAI"),
    ("anthropic", "Anthropic"),
    ("google", "Google"),
    ("deepmind", "DeepMind"),
    ("meta", "Meta"),
    ("microsoft", "Microsoft"),
    ("nvidia", "NVIDIA"),
    ("apple", "Apple"),
    ("amazon", "Amazon"),
    ("hugging face", "Hugging Face"),
    ("stability ai", "Stability AI"),
    ("mistral", "Mistral AI"),
    ("cohere", "Cohere"),
    ("databricks", "Databricks"),
    ("tesla", "Tesla"),
    ("baidu", "Baidu"),
    ("alibaba", "Alibaba"),
    ("tencent", "Tencent"),
    ("samsung", "Samsung"),
    ("ibm", "IBM"),
]

# Technology patterns for entity extraction
_TECH_PATTERNS: list[tuple[str, str]] = [
    ("large language model", "LLM"),
    ("llm", "LLM"),
    ("transformer", "Transformer"),
    ("diffusion model", "Diffusion Model"),
    ("reinforcement learning", "Reinforcement Learning"),
    ("rlhf", "RLHF"),
    ("fine-tun", "Fine-tuning"),
    ("neural network", "Neural Network"),
    ("computer vision", "Computer Vision"),
    ("natural language processing", "NLP"),
    ("nlp", "NLP"),
    ("generative ai", "Generative AI"),
    ("multimodal", "Multimodal AI"),
    ("robotics", "Robotics"),
    ("autonomous", "Autonomous Systems"),
    ("speech recognition", "Speech Recognition"),
    ("text to speech", "Text-to-Speech"),
    ("retrieval augmented", "RAG"),
    ("rag", "RAG"),
    ("agent", "AI Agents"),
    ("agentic", "AI Agents"),
]

# Pillar classification patterns
_PILLAR_CONCEPTS: list[tuple[str, str]] = [
    # ai_tools indicators
    ("free tool", "ai_tools"),
    ("how to use", "ai_tools"),
    ("tutorial", "ai_tools"),
    ("api", "ai_tools"),
    ("developer", "ai_tools"),
    ("productivity", "ai_tools"),
    ("workflow", "ai_tools"),
    ("open source tool", "ai_tools"),
    # ai_concepts indicators
    ("explained", "ai_concepts"),
    ("how does", "ai_concepts"),
    ("what is", "ai_concepts"),
    ("understand", "ai_concepts"),
    ("deep dive", "ai_concepts"),
    ("fundamentals", "ai_concepts"),
    ("introduction to", "ai_concepts"),
    # ai_daily_life indicators
    ("daily life", "ai_daily_life"),
    ("healthcare", "ai_daily_life"),
    ("education", "ai_daily_life"),
    ("job market", "ai_daily_life"),
    ("consumer", "ai_daily_life"),
    ("personal", "ai_daily_life"),
    ("everyday", "ai_daily_life"),
    # ai_news is the default
    ("announces", "ai_news"),
    ("launches", "ai_news"),
    ("releases", "ai_news"),
    ("breaking", "ai_news"),
    ("raises", "ai_news"),
    ("acquires", "ai_news"),
]


# ---------------------------------------------------------------------------
# Fuzzy matching (adapted from fuzzy_pdf_parser._fuzzy_match_concept)
# ---------------------------------------------------------------------------

def _fuzzy_match(
    text: str,
    concepts: list[tuple[str, str]],
    threshold: float = 0.6,
) -> Optional[str]:
    """Find the best matching concept for text using fuzzy matching.

    Adapted from fuzzy_pdf_parser._fuzzy_match_concept:
    1. Try exact substring match first (fast path)
    2. Fall back to rapidfuzz WRatio if available
    3. Fall back to difflib SequenceMatcher

    Returns the canonical name, or None if no match.
    """
    text_lower = text.lower()

    # Fast path: exact substring match
    for pattern, canonical in concepts:
        if pattern in text_lower:
            return canonical

    # Try rapidfuzz (faster and more accurate)
    try:
        from rapidfuzz import process, fuzz
        # Filter out very short patterns that cause false positives with WRatio
        # (e.g. "chip" matching "pizza" via partial ratio).
        filtered = {
            pattern: canonical
            for pattern, canonical in concepts
            if not (len(pattern) < len(text_lower) * 0.3 and len(pattern) < 8)
        }
        if filtered:
            result = process.extractOne(
                text_lower,
                filtered.keys(),
                scorer=fuzz.WRatio,
                score_cutoff=threshold * 100,
            )
            if result is not None:
                matched_pattern, score, _ = result
                return filtered[matched_pattern]
        return None
    except ImportError:
        pass

    # Fallback: difflib SequenceMatcher
    # Guard: skip comparisons where the pattern is much shorter than the text.
    # SequenceMatcher produces false positives when comparing a long string
    # against a very short pattern (e.g. "best pizza recipes" vs "chip").
    best_ratio = 0.0
    best_canonical = None
    for pattern, canonical in concepts:
        # Skip if length ratio is too skewed (pattern < 30% of text length)
        if len(pattern) < len(text_lower) * 0.3 and len(pattern) < 8:
            continue
        ratio = SequenceMatcher(None, text_lower, pattern).ratio()
        if ratio > best_ratio and ratio >= threshold:
            best_ratio = ratio
            best_canonical = canonical

    return best_canonical


def _extract_all_matches(
    text: str,
    patterns: list[tuple[str, str]],
) -> list[str]:
    """Extract all matching entities from text (companies, technologies).

    Like fuzzy_pdf_parser's row-by-row extraction, but for entity mentions.
    """
    text_lower = text.lower()
    found = []
    seen = set()
    for pattern, canonical in patterns:
        if pattern in text_lower and canonical not in seen:
            found.append(canonical)
            seen.add(canonical)
    return found


# ---------------------------------------------------------------------------
# Tier 1: Fuzzy classifier (FREE, no API key needed)
# Adapted from fuzzy_pdf_parser.extract_financials_from_pdf
# ---------------------------------------------------------------------------

def fuzzy_classify(title: str, summary: str = "") -> ExtractionResult:
    """Classify and extract article insights using fuzzy matching only (FREE).

    Adapted from fuzzy_pdf_parser's approach:
    - Concept dictionaries instead of LLM
    - Fuzzy string matching for category detection
    - Entity extraction via pattern matching
    - Heuristic scoring based on keyword density

    This is Tier 1 (free fallback). No API key required.
    """
    combined = f"{title} {summary}"
    result = ExtractionResult(title=title, source_method="fuzzy")

    # Category classification (like _fuzzy_match_concept against _INCOME_CONCEPTS)
    result.category = _fuzzy_match(combined, _NEWS_CONCEPTS, threshold=0.55) or "general"

    # Pillar classification: check news indicators FIRST (they're more specific)
    # then fall back to concept/tool/life indicators
    pillar = _fuzzy_match(combined, _PILLAR_CONCEPTS, threshold=0.5)
    # Category-based override: model_release/acquisition/funding -> ai_news
    if result.category in ("model_release", "acquisition", "funding", "regulation", "safety"):
        pillar = "ai_news"
    elif result.category == "tool_launch":
        pillar = "ai_tools"
    result.pillar = pillar or "ai_news"

    # Entity extraction (like extracting financial line items)
    result.companies_mentioned = _extract_all_matches(combined, _COMPANY_PATTERNS)
    result.technologies_mentioned = _extract_all_matches(combined, _TECH_PATTERNS)

    # Sentiment detection (simple keyword-based)
    positive_words = {"breakthrough", "launches", "improves", "free", "open source",
                      "record", "first", "innovative", "revolutionary", "amazing"}
    negative_words = {"ban", "layoff", "concern", "risk", "threat", "fails",
                      "lawsuit", "controversy", "dangerous", "warning"}
    text_lower = combined.lower()
    pos = sum(1 for w in positive_words if w in text_lower)
    neg = sum(1 for w in negative_words if w in text_lower)
    if pos > neg:
        result.sentiment = "positive"
    elif neg > pos:
        result.sentiment = "negative"
    else:
        result.sentiment = "neutral"

    # Impact score heuristic (like fuzzy_pdf_parser's value extraction)
    score = 5.0  # base
    score += min(len(result.companies_mentioned) * 0.5, 2.0)
    score += min(len(result.technologies_mentioned) * 0.3, 1.5)
    if result.category in ("model_release", "acquisition", "regulation"):
        score += 1.5
    if result.sentiment == "positive":
        score += 0.5
    result.impact_score = min(score, 10.0)

    # Audience relevance (US tech audience)
    relevance = 5.0
    us_companies = {"OpenAI", "Anthropic", "Google", "Meta", "Microsoft",
                    "NVIDIA", "Apple", "Amazon"}
    if any(c in us_companies for c in result.companies_mentioned):
        relevance += 2.0
    if result.category in ("tool_launch", "model_release"):
        relevance += 1.5
    result.audience_relevance = min(relevance, 10.0)

    # Key facts (extract sentences with entity mentions)
    sentences = re.split(r'[.!?]+', summary)
    for sent in sentences[:5]:
        sent = sent.strip()
        if len(sent) > 20 and any(p in sent.lower() for p, _ in _COMPANY_PATTERNS[:10]):
            result.key_facts.append(sent[:150])
    if not result.key_facts and summary:
        result.key_facts = [summary[:150]]

    result.success = True
    return result


# ---------------------------------------------------------------------------
# Tier 2: LLM extractor (paid, deeper analysis)
# Adapted from llm_filing_extractor.LLMFilingExtractor
# ---------------------------------------------------------------------------

_LLM_EXTRACTION_PROMPT = """You are the editorial AI for Bolt, an AI robot news creator.
Analyze this article and extract structured insights for short-form video creation.

{taxonomy_hint}

ARTICLE TITLE: {title}
ARTICLE TEXT: {summary}

Extract the following and respond ONLY with valid JSON:
{{
    "pillar": "ai_news | ai_tools | ai_concepts | ai_daily_life",
    "category": "model_release | funding | acquisition | regulation | safety | research | tool_launch | hardware | industry_impact | partnership | general",
    "impact_score": 0-10 (how impactful for short-form video),
    "sentiment": "positive | neutral | negative",
    "key_facts": ["fact 1", "fact 2", "fact 3"],
    "hook_idea": "One compelling sentence to open a 45-second video",
    "companies": ["Company1", "Company2"],
    "technologies": ["Tech1", "Tech2"],
    "audience_relevance": 0-10 (relevance to US tech-curious audience)
}}"""


def llm_extract(title: str, summary: str, config: dict) -> ExtractionResult:
    """Extract structured article insights using Claude LLM.

    Adapted from llm_filing_extractor.LLMFilingExtractor:
    - Per-pillar taxonomy hints (like per-market hints)
    - Structured JSON extraction prompt
    - Validation of extracted data
    - Fallback to fuzzy classifier on failure

    This is Tier 2 (paid). Requires ANTHROPIC_API_KEY.
    """
    api_key = config.get("apis", {}).get("anthropic_api_key", "")
    if not api_key or api_key.startswith("YOUR_") or api_key.startswith("\u2192"):
        logger.debug("No Anthropic API key -- falling back to fuzzy classifier")
        return fuzzy_classify(title, summary)

    # Determine pillar hint (like market taxonomy selection)
    fuzzy_result = fuzzy_classify(title, summary)
    pillar_hint = _PILLAR_TAXONOMY_HINTS.get(fuzzy_result.pillar,
                                              _PILLAR_TAXONOMY_HINTS["ai_news"])

    prompt = _LLM_EXTRACTION_PROMPT.format(
        taxonomy_hint=pillar_hint,
        title=title,
        summary=summary[:1000],
    )

    try:
        import anthropic  # noqa: local import for lazy loading
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=config.get("apis", {}).get("anthropic_model", "claude-sonnet-4-20250514"),
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text
        raw = re.sub(r"```json|```", "", raw).strip()
        data = json.loads(raw)

        result = ExtractionResult(
            title=title,
            pillar=data.get("pillar", "ai_news"),
            category=data.get("category", "general"),
            impact_score=float(data.get("impact_score", 5.0)),
            sentiment=data.get("sentiment", "neutral"),
            key_facts=data.get("key_facts", []),
            hook_idea=data.get("hook_idea", ""),
            companies_mentioned=data.get("companies", []),
            technologies_mentioned=data.get("technologies", []),
            audience_relevance=float(data.get("audience_relevance", 5.0)),
            source_method="llm_claude",
            success=True,
        )

        # Validation (like accounting identity checks in llm_filing_extractor)
        _validate_extraction(result)

        logger.info(
            "LLM extraction: pillar=%s category=%s impact=%.1f sentiment=%s",
            result.pillar, result.category, result.impact_score, result.sentiment,
        )
        return result

    except json.JSONDecodeError as e:
        logger.warning("LLM returned invalid JSON: %s. Falling back to fuzzy.", e)
        return fuzzy_classify(title, summary)
    except Exception as e:
        logger.warning("LLM extraction failed: %s. Falling back to fuzzy.", e)
        return fuzzy_classify(title, summary)


def _validate_extraction(result: ExtractionResult) -> None:
    """Validate extraction consistency (like accounting identity checks).

    Adapted from llm_filing_extractor._validate_accounting_identities:
    - Check score ranges
    - Check pillar/category consistency
    - Clamp values to valid ranges
    """
    # Clamp scores
    result.impact_score = max(0.0, min(10.0, result.impact_score))
    result.audience_relevance = max(0.0, min(10.0, result.audience_relevance))

    # Validate pillar
    valid_pillars = {"ai_news", "ai_tools", "ai_concepts", "ai_daily_life"}
    if result.pillar not in valid_pillars:
        result.pillar = "ai_news"

    # Validate sentiment
    if result.sentiment not in ("positive", "neutral", "negative"):
        result.sentiment = "neutral"

    # Consistency check: tool_launch category should map to ai_tools pillar
    if result.category == "tool_launch" and result.pillar == "ai_news":
        result.pillar = "ai_tools"


# ---------------------------------------------------------------------------
# Main entry point: tiered extraction (like HKEX akshare -> scraper)
# ---------------------------------------------------------------------------

def extract_article(
    title: str,
    summary: str = "",
    config: Optional[dict] = None,
    force_fuzzy: bool = False,
) -> ExtractionResult:
    """Extract structured insights from an article using tiered approach.

    Tier 1 (FREE): Fuzzy classifier -- pattern matching, no API key needed
    Tier 2 (PAID): Claude LLM extractor -- deep analysis, needs API key

    Like HKEX's akshare (fast/free) -> filing scraper (slow/thorough).

    Parameters
    ----------
    title : str
        Article title.
    summary : str
        Article body/summary text.
    config : dict, optional
        Bolt config dict (for API keys). If None, uses fuzzy only.
    force_fuzzy : bool
        If True, skip LLM even if API key is available.

    Returns
    -------
    ExtractionResult with structured insights.
    """
    if force_fuzzy or config is None:
        return fuzzy_classify(title, summary)

    # Try LLM first (will fall back to fuzzy internally if no key)
    return llm_extract(title, summary, config)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

    test_articles = [
        ("OpenAI launches GPT-5 with revolutionary reasoning capabilities",
         "OpenAI has announced GPT-5, featuring major improvements in multi-step reasoning, "
         "code generation, and multimodal understanding. The model uses 90% less energy than GPT-4."),
        ("Free AI tool lets anyone create professional videos in minutes",
         "A new open source tool called VidGen uses diffusion models to generate professional "
         "quality videos from text prompts. No GPU required -- runs in the cloud for free."),
        ("What is RLHF and why does it matter?",
         "Reinforcement Learning from Human Feedback is the technique that made ChatGPT useful. "
         "Here's how it works in simple terms and why every AI company is investing in it."),
        ("AI is changing how doctors diagnose cancer",
         "Hospitals across the US are adopting AI systems that can detect cancer earlier than "
         "human radiologists. The technology has already saved thousands of lives."),
    ]

    print(f"\n{'='*65}")
    print("  Content Extractor -- Fuzzy Classification (FREE tier)")
    print(f"{'='*65}")

    for title, summary in test_articles:
        result = extract_article(title, summary, force_fuzzy=True)
        print(f"\n  Title:      {title[:60]}")
        print(f"  Pillar:     {result.pillar}")
        print(f"  Category:   {result.category}")
        print(f"  Impact:     {result.impact_score:.1f}/10")
        print(f"  Sentiment:  {result.sentiment}")
        print(f"  Companies:  {', '.join(result.companies_mentioned) or 'none'}")
        print(f"  Tech:       {', '.join(result.technologies_mentioned) or 'none'}")
        print(f"  Relevance:  {result.audience_relevance:.1f}/10")
        print(f"  Method:     {result.source_method}")

    print(f"\n{'='*65}\n")

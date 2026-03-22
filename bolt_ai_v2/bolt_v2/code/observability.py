#!/usr/bin/env python3
"""
Bolt AI — Observability: Structured Logging + API Rate Limiter
==============================================================

observability.py provides two things:

1. STRUCTURED LOGGING
   All log messages are emitted as JSON with consistent fields:
     timestamp, level, logger, message, context...
   This makes logs searchable in any log viewer (Grafana, Datadog,
   CloudWatch, or even just `jq`).

   Usage:
     from observability import get_logger
     logger = get_logger("bolt.myscript")
     logger.info("Script generated", extra={"score": 9.1, "words": 112})

2. API RATE LIMITER (Token Bucket)
   Prevents hitting API rate limits by throttling calls per service.
   Each service has its own token bucket that refills at a configured rate.

   Usage:
     from observability import rate_limiter
     await rate_limiter.acquire("claude")       # blocks until a slot is free
     response = client.messages.create(...)

   Config (in config.json under "rate_limiting"):
     "claude":       50 req/min  (Anthropic tier-1 default)
     "elevenlabs":   20 req/min  (free tier)
     "edge_tts":    200 req/min  (effectively unlimited)
     "vidnoz":       10 req/min  (conservative)
     "did":          10 req/min
     "youtube":      50 req/min
     "buffer":       20 req/min
"""

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ══════════════════════════════════════════════════════════════════
# STRUCTURED LOGGING
# ══════════════════════════════════════════════════════════════════

class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "ts":      datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level":   record.levelname,
            "logger":  record.name,
            "msg":     record.getMessage(),
        }
        # Include any extra fields passed via logger.info(..., extra={...})
        for key, val in record.__dict__.items():
            if key not in ("name","msg","args","created","filename","funcName",
                           "levelname","levelno","lineno","module","msecs",
                           "pathname","process","processName","relativeCreated",
                           "stack_info","taskName","thread","threadName",
                           "exc_info","exc_text","message","asctime"):
                doc[key] = val
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str)


class HumanFormatter(logging.Formatter):
    """Human-readable format for console output."""
    LEVEL_ICONS = {
        "DEBUG": "·", "INFO": "ℹ", "WARNING": "⚠",
        "ERROR": "✗", "CRITICAL": "☠",
    }
    def format(self, record: logging.LogRecord) -> str:
        icon = self.LEVEL_ICONS.get(record.levelname, "·")
        ts   = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
        name = record.name.replace("bolt.", "")
        msg  = record.getMessage()
        base = f"{ts} {icon} [{name}] {msg}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup_logging(
    log_dir: str = "logs",
    level: str = "INFO",
    json_file: bool = True,
    human_console: bool = True,
) -> None:
    """
    Configure root logger with:
      - JSON file handler (machine-readable, structured)
      - Human-readable console handler (coloured, easy to read)

    Call this once at startup in content_automation_master.py.
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    if json_file:
        from logging.handlers import RotatingFileHandler
        date_str = datetime.now().strftime("%Y%m%d")
        # RotatingFileHandler: max 10 MB per file, keep 5 backups (50 MB total)
        fh = RotatingFileHandler(
            log_path / f"bolt_{date_str}.jsonl",
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        fh.setFormatter(JSONFormatter())
        fh.setLevel(logging.DEBUG)
        root.addHandler(fh)

    if human_console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(HumanFormatter())
        ch.setLevel(getattr(logging, level.upper(), logging.INFO))
        root.addHandler(ch)


def get_logger(name: str) -> logging.Logger:
    """Get a named logger. Use: logger = get_logger('bolt.myscript')"""
    return logging.getLogger(name)


# ══════════════════════════════════════════════════════════════════
# TOKEN BUCKET RATE LIMITER
# ══════════════════════════════════════════════════════════════════

class TokenBucket:
    """
    Token bucket rate limiter for a single API service.

    Tokens refill at `rate` per second up to `capacity`.
    Each API call consumes 1 token. If the bucket is empty,
    the caller waits until a token is available.

    This prevents:
      - HTTP 429 (Too Many Requests) errors
      - Exponential backoff storms when multiple steps run concurrently
      - Wasting retry quota on rate-limit errors that were preventable
    """

    def __init__(self, service: str, requests_per_minute: float, burst: int = 5):
        self.service  = service
        self.rate     = requests_per_minute / 60.0   # tokens per second
        self.capacity = max(burst, int(requests_per_minute / 10))
        self.tokens   = float(self.capacity)
        self._last_refill = time.monotonic()
        self._lock    = asyncio.Lock()
        self._logger  = logging.getLogger(f"bolt.ratelimiter.{service}")

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        gained  = elapsed * self.rate
        self.tokens = min(self.capacity, self.tokens + gained)
        self._last_refill = now

    async def acquire(self, tokens: float = 1.0) -> None:
        """
        Acquire `tokens` from the bucket, blocking until available.
        Async-safe. Call before every API request.
        """
        async with self._lock:
            while True:
                self._refill()
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return
                # Calculate how long to wait
                deficit  = tokens - self.tokens
                wait_sec = deficit / self.rate
                self._logger.debug(
                    f"Rate limit: waiting {wait_sec:.1f}s for {self.service}",
                    extra={"service": self.service, "wait_seconds": round(wait_sec, 2)}
                )
                await asyncio.sleep(min(wait_sec + 0.1, 60.0))

    def acquire_sync(self, tokens: float = 1.0) -> None:
        """Synchronous version — use in non-async code."""
        while True:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return
            deficit  = tokens - self.tokens
            wait_sec = deficit / self.rate
            self._logger.debug(f"Rate limit sync: waiting {wait_sec:.1f}s for {self.service}")
            time.sleep(min(wait_sec + 0.1, 60.0))

    @property
    def available(self) -> float:
        self._refill()
        return self.tokens


class RateLimiterRegistry:
    """
    Registry of per-service token buckets.
    Loaded from config.json → advanced → rate_limiting.

    Default limits (conservative, stays within free tiers):
      claude:      50 req/min
      elevenlabs:  20 req/min (free tier: 10K chars/mo, not req/min)
      edge_tts:   200 req/min (effectively unlimited)
      google_tts:  20 req/min (free tier is very generous)
      vidnoz:      10 req/min
      did:         10 req/min
      youtube:     50 req/min (YouTube API units)
      buffer:      20 req/min
      instagram:   10 req/min (strict Graph API limit)
      tiktok:      10 req/min
    """

    _DEFAULTS: dict[str, int] = {
        "claude":      50,
        "elevenlabs":  20,
        "edge_tts":   200,
        "google_tts":  20,
        "vidnoz":      10,
        "did":         10,
        "youtube":     50,
        "buffer":      20,
        "instagram":   10,
        "tiktok":      10,
        "discord":     30,
    }

    def __init__(self, config: Optional[dict] = None):
        self._buckets: dict[str, TokenBucket] = {}
        self._config  = config or {}
        self._logger  = logging.getLogger("bolt.ratelimiter")
        self._load_from_config()

    def _load_from_config(self) -> None:
        raw_limits = (
            self._config.get("advanced", {})
                        .get("rate_limiting", {})
        )
        # Normalize config keys: "claude_requests_per_minute" -> "claude"
        limits = {}
        for k, v in raw_limits.items():
            if k in ("enabled",):
                continue  # Skip non-service keys
            normalized = k.replace("_requests_per_minute", "")
            limits[normalized] = v
        combined = {**self._DEFAULTS, **limits}
        for service, rpm in combined.items():
            if isinstance(rpm, (int, float)) and rpm > 0:
                self._buckets[service] = TokenBucket(service, float(rpm))
        self._logger.info(
            f"Rate limiter ready: {len(self._buckets)} services",
            extra={"services": list(self._buckets.keys())}
        )

    def get(self, service: str) -> TokenBucket:
        """Get or create a token bucket for a service."""
        if service not in self._buckets:
            rpm = self._DEFAULTS.get(service, 10)
            self._buckets[service] = TokenBucket(service, float(rpm))
            self._logger.debug(f"Created bucket for {service}: {rpm} req/min")
        return self._buckets[service]

    async def acquire(self, service: str, tokens: float = 1.0) -> None:
        """Async acquire — use before any async API call."""
        await self.get(service).acquire(tokens)

    def acquire_sync(self, service: str, tokens: float = 1.0) -> None:
        """Sync acquire — use before any synchronous API call."""
        self.get(service).acquire_sync(tokens)

    def status(self) -> dict:
        """Return current token availability per service."""
        return {svc: round(b.available, 1) for svc, b in self._buckets.items()}


# ── Module-level singletons ────────────────────────────────────────────────

_rate_limiter: Optional[RateLimiterRegistry] = None


def get_rate_limiter(config: Optional[dict] = None) -> RateLimiterRegistry:
    """Get or create the module-level rate limiter singleton."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiterRegistry(config)
    return _rate_limiter


# Convenience alias — import and use directly:
#   from observability import rate_limiter
#   await rate_limiter.acquire("claude")
rate_limiter = None   # Initialised lazily on first import with config


def init(config: dict, log_dir: str = "logs", log_level: str = "INFO") -> None:
    """
    Initialise both subsystems at once. Call once at startup:
      from observability import init
      init(config)
    """
    global rate_limiter
    setup_logging(log_dir=log_dir, level=log_level)
    rate_limiter = get_rate_limiter(config)

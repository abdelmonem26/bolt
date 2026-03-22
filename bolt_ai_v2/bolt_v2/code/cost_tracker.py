#!/usr/bin/env python3
"""
Bolt AI — Cost Tracker Module
Tracks API usage and estimates costs for the content pipeline.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List
from collections import defaultdict

logger = logging.getLogger("bolt.costtracker")

# Default pricing fallback (used only when config.json is unavailable)
# Update config.json -> cost_tracking -> pricing_usd to change these.
_DEFAULT_PRICING: dict = {
    "claude-3-haiku_input_1k":    0.00025,
    "claude-3-haiku_output_1k":   0.00125,
    "claude-3-sonnet_input_1k":   0.003,
    "claude-3-sonnet_output_1k":  0.015,
    "claude-3-5-sonnet_input_1k": 0.003,
    "claude-3-5-sonnet_output_1k":0.015,
    "elevenlabs_1k_chars":        0.001,
    "heygen_1_minute":            0.04,
    "vidnoz_1_video":             0.0,    # Free plan
    "did_1_video":                0.0,    # Free plan (20/month)
    "creatomate_per_render":      0.01,
    "google_tts_1k_chars_neural": 0.000016,
    "google_tts_1k_chars_std":    0.000004,
    "edge_tts_1k_chars":          0.0,    # Completely free
    "youtube_upload":             0.0,    # Free API
    "buffer_post":                0.0,    # Free plan
}


class CostTracker:
    """Track and estimate costs for the Bolt AI pipeline.

    Primary storage: SQLite ``cost_events`` table (via database.BoltDB).
    Secondary storage: ``data/analytics/cost_tracking.json`` (deprecated,
    kept for backward compatibility with dashboard static file reads).

    The DB is the source of truth. The JSON file is written as a convenience
    but should not be relied upon for cost calculations.
    """

    def __init__(self, config_path: str = "code/config.json"):
        self.config_path = config_path
        self.data_dir = Path("data/analytics")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.costs_file = self.data_dir / "cost_tracking.json"
        self.costs = self._load_costs()

        # Attempt to get DB handle for primary storage
        self._db = None
        try:
            from database import get_db
            self._db = get_db()
        except Exception:
            logger.debug("DB unavailable for cost tracking -- using JSON file only")
        
    def _load_costs(self) -> Dict:
        """Load existing cost data."""
        if self.costs_file.exists():
            with open(self.costs_file) as f:
                return json.load(f)
        return {
            "daily": [],
            "monthly": {},
            "total_spent": 0.0,
            "total_videos": 0,
            "last_updated": datetime.now(timezone.utc).isoformat()
        }
    
    def _save_costs(self) -> None:
        """Save cost data."""
        self.costs["last_updated"] = datetime.now(timezone.utc).isoformat()
        with open(self.costs_file, 'w') as f:
            json.dump(self.costs, f, indent=2)
    
    def record_usage(self, service: str, operation: str, quantity: float,
                    model: str = None) -> None:
        """Record API usage.

        Writes to the SQLite ``cost_events`` table (primary) and the legacy
        JSON file (secondary).
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        date = datetime.now().strftime("%Y-%m-%d")

        # Calculate cost
        cost = self._calculate_cost(service, operation, quantity, model)

        # ── Primary: write to DB ───────────────────────────────────────
        if self._db is not None:
            try:
                self._db.record_cost(service, operation, cost, model=model)
            except Exception as exc:
                logger.warning(f"DB cost write failed ({exc}) -- falling back to JSON only")

        # ── Secondary (deprecated): write to JSON file ─────────────────
        daily_entry = {
            "timestamp": timestamp,
            "service": service,
            "operation": operation,
            "quantity": quantity,
            "cost": cost,
            "model": model
        }

        self.costs["daily"].append(daily_entry)

        # Keep only last 90 days of daily data
        if len(self.costs["daily"]) > 1000:
            self.costs["daily"] = self.costs["daily"][-1000:]

        # Update monthly totals
        if date not in self.costs["monthly"]:
            self.costs["monthly"][date] = {
                "total_cost": 0.0,
                "services": defaultdict(float),
                "videos": 0
            }

        self.costs["monthly"][date]["total_cost"] += cost
        self.costs["monthly"][date]["services"][service] += cost

        # Update totals
        self.costs["total_spent"] += cost

        self._save_costs()

        logger.info(f"Recorded: {service}/{operation} = ${cost:.4f}")
    
    def _load_pricing(self) -> dict:
        """Load pricing from config.json, falling back to defaults if unavailable."""
        try:
            with open(self.config_path) as f:
                cfg = json.load(f)
            pricing = cfg.get("cost_tracking", {}).get("pricing_usd", {})
            if pricing:
                return {**_DEFAULT_PRICING, **pricing}  # config overrides defaults
        except Exception:
            pass
        return _DEFAULT_PRICING

    def _calculate_cost(self, service: str, operation: str,
                        quantity: float, model: str = None) -> float:
        """
        Calculate cost dynamically — reads pricing_usd from config.json on every call
        so that price changes only require editing config.json, never code.

        quantity units:
          claude:      thousands of tokens (input_tokens / 1000)
          elevenlabs:  characters
          edge_tts:    characters (always $0)
          google_tts:  characters
          heygen:      minutes of video
          vidnoz:      number of videos
          did:         number of videos
          youtube:     number of uploads
          buffer:      number of posts
        """
        rates = self._load_pricing()

        if service == "claude":
            # model examples: "claude-3-haiku", "claude-3-sonnet", "claude-3-5-sonnet"
            is_output = "output" in operation
            suffix = "_output_1k" if is_output else "_input_1k"
            key = f"{model}{suffix}" if model else None
            rate = rates.get(key, rates.get("claude-3-sonnet_output_1k" if is_output else "claude-3-sonnet_input_1k", 0.003))
            return quantity * rate  # quantity = token_count / 1000

        elif service == "elevenlabs":
            chars_k = quantity / 1000
            return chars_k * rates.get("elevenlabs_1k_chars", 0.001)

        elif service == "edge_tts":
            return 0.0  # Always free

        elif service == "google_tts":
            neural = "neural" in operation
            rate_key = "google_tts_1k_chars_neural" if neural else "google_tts_1k_chars_std"
            chars_k = quantity / 1000
            return chars_k * rates.get(rate_key, 0.000016)

        elif service == "heygen":
            return quantity * rates.get("heygen_1_minute", 0.04)

        elif service == "vidnoz":
            return rates.get("vidnoz_1_video", 0.0)  # Free plan

        elif service == "did":
            return rates.get("did_1_video", 0.0)  # Free plan

        elif service == "creatomate":
            return rates.get("creatomate_per_render", 0.01)

        elif service == "youtube":
            return rates.get("youtube_upload", 0.0)

        elif service == "buffer":
            return rates.get("buffer_post", 0.0)

        return 0.0
    
    def get_daily_summary(self, date: str = None) -> Dict:
        """Get cost summary for a specific date."""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        
        if date not in self.costs["monthly"]:
            return {
                "date": date,
                "total_cost": 0.0,
                "services": {},
                "videos": 0
            }
        
        monthly = self.costs["monthly"][date]
        return {
            "date": date,
            "total_cost": monthly["total_cost"],
            "services": dict(monthly["services"]),
            "videos": monthly.get("videos", 0)
        }
    
    def get_monthly_summary(self, year_month: str = None) -> Dict:
        """Get cost summary for a month (format: YYYY-MM)."""
        if year_month is None:
            year_month = datetime.now().strftime("%Y-%m")
        
        total_cost = 0.0
        services = defaultdict(float)
        videos = 0
        
        for date, data in self.costs["monthly"].items():
            if date.startswith(year_month):
                total_cost += data["total_cost"]
                for service, cost in data["services"].items():
                    services[service] += cost
                videos += data.get("videos", 0)
        
        return {
            "month": year_month,
            "total_cost": total_cost,
            "services": dict(services),
            "videos": videos,
            "avg_cost_per_video": total_cost / videos if videos > 0 else 0
        }
    
    def get_total_summary(self) -> Dict:
        """Get total cost summary."""
        return {
            "total_spent": self.costs["total_spent"],
            "total_videos": self.costs.get("total_videos", 0),
            "avg_cost_per_video": (
                self.costs["total_spent"] / self.costs.get("total_videos", 1)
                if self.costs.get("total_videos", 0) > 0 else 0
            ),
            "last_updated": self.costs.get("last_updated")
        }
    
    def increment_video_count(self) -> None:
        """Increment the total video count."""
        self.costs["total_videos"] = self.costs.get("total_videos", 0) + 1
        date = datetime.now().strftime("%Y-%m-%d")
        if date in self.costs["monthly"]:
            self.costs["monthly"][date]["videos"] = (
                self.costs["monthly"][date].get("videos", 0) + 1
            )
        self._save_costs()
    
    def export_report(self, days: int = 30) -> Dict:
        """Export cost report for the last N days."""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        daily_costs = []
        total = 0.0
        
        for i in range(days):
            date = (start_date + timedelta(days=i)).strftime("%Y-%m-%d")
            if date in self.costs["monthly"]:
                data = self.costs["monthly"][date]
                daily_costs.append({
                    "date": date,
                    "cost": data["total_cost"],
                    "videos": data.get("videos", 0)
                })
                total += data["total_cost"]
        
        return {
            "period_days": days,
            "total_cost": total,
            "avg_daily_cost": total / days if days > 0 else 0,
            "daily_breakdown": daily_costs
        }


# Convenience functions for recording specific operations
def record_claude_tokens(input_tokens: int, output_tokens: int, model: str = "claude-3-haiku"):
    """Record Claude API usage."""
    tracker = CostTracker()
    tracker.record_usage("claude", "input", input_tokens / 1000, model)
    tracker.record_usage("claude", "output", output_tokens / 1000, f"{model}_output")


def record_elevenlabs_chars(chars: int):
    """Record ElevenLabs TTS usage."""
    tracker = CostTracker()
    tracker.record_usage("elevenlabs", "tts", chars)


def record_heygen_minutes(minutes: float):
    """Record HeyGen video generation."""
    tracker = CostTracker()
    tracker.record_usage("heygen", "avatar_video", minutes)


def record_google_tts_chars(chars: int):
    """Record Google TTS usage."""
    tracker = CostTracker()
    tracker.record_usage("google_tts", "standard", chars)


def record_video_complete():
    """Mark a video as completed (increments counter)."""
    tracker = CostTracker()
    tracker.increment_video_count()


if __name__ == "__main__":
    # Test the cost tracker
    tracker = CostTracker()
    
    # Record some test data
    tracker.record_usage("claude", "input", 1000, "claude-3-haiku")
    tracker.record_usage("claude", "output", 500, "claude-3-haiku")
    tracker.record_usage("elevenlabs", "tts", 5000)
    tracker.record_usage("heygen", "avatar_video", 0.5)
    tracker.increment_video_count()
    
    print("📊 Cost Tracking Test:")
    print(f"  Daily: {tracker.get_daily_summary()}")
    print(f"  Monthly: {tracker.get_monthly_summary()}")
    print(f"  Total: {tracker.get_total_summary()}")

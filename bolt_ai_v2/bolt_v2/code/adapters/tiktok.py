"""
TikTok adapter -- transform and publish for TikTok.

Pre-plan Section 20:
  - Transform: same master file, captions burned in for silent autoplay
  - Metadata: hook + 2 key facts + catchphrase + 3-5 hashtags maximum
  - Publish: Buffer API primary, TikTok Content Posting API direct when approved
"""

import logging
import subprocess
from pathlib import Path

import requests

try:
    from . import PlatformAdapter, PlatformPackage, PublicationResult
except ImportError:
    from adapters import PlatformAdapter, PlatformPackage, PublicationResult

logger = logging.getLogger("bolt.dist.tiktok")


class TikTokAdapter(PlatformAdapter):
    platform_name = "tiktok"

    def transform(self, master_video_path: str, script: dict,
                  article: dict, config: dict) -> PlatformPackage:
        """TikTok: burn captions into video for silent autoplay, 3-5 hashtags max."""
        from caption_composer import compose_caption

        captions = compose_caption(script, article, "tiktok", config)

        # Burn captions into video for silent autoplay (pre-plan requirement)
        captioned_path = self._burn_captions(master_video_path, script.get("script", ""), config)

        return PlatformPackage(
            platform="tiktok",
            video_path=captioned_path or master_video_path,
            caption=captions["caption"],
            hashtags=captions["hashtags"][:5],  # Pre-plan: 3-5 hashtags max
        )

    def _burn_captions(self, video_path: str, script_text: str, config: dict) -> str:
        """Use FFmpeg to burn subtitle captions into the video for silent autoplay."""
        out_path = str(Path(video_path).with_suffix("")) + "_captioned.mp4"
        # Create a simple subtitle line from the first 60 chars of the script
        subtitle_text = script_text[:80].replace("'", "\\'").replace('"', '\\"')
        cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-vf", f"drawtext=text='{subtitle_text}':fontcolor=white:fontsize=32:"
                   f"x=(w-tw)/2:y=h-th-100:box=1:boxcolor=black@0.6:boxborderw=8",
            "-c:a", "copy", "-c:v", "libx264", "-preset", "fast",
            out_path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                logger.info(f"TikTok captions burned: {Path(out_path).name}")
                return out_path
            logger.warning(f"Caption burn failed: {result.stderr[-200:]}")
        except Exception as e:
            logger.warning(f"Caption burn error: {e}")
        return ""  # Fall back to uncaptioned

    def publish(self, package: PlatformPackage, config: dict) -> PublicationResult:
        """Publish via Buffer API."""
        try:
            access_token = config.get("apis", {}).get("buffer_access_token", "")
            if not access_token or access_token.startswith("YOUR_"):
                return PublicationResult(platform="tiktok", success=False, error="Buffer not configured")

            from buffer_utils import get_buffer_profile_ids, schedule_via_buffer
            profiles = get_buffer_profile_ids(access_token)
            if "tiktok" not in profiles:
                return PublicationResult(platform="tiktok", success=False, error="No TikTok Buffer profile")

            post_time = config.get("platforms", {}).get("tiktok", {}).get("post_time", "19:00")
            result = schedule_via_buffer(
                package.video_path, {"captions": {"tiktok": {"caption": package.caption, "hashtags": package.hashtags}},
                                      "article": {"title": package.caption[:60]}},
                "tiktok", profiles["tiktok"], access_token, post_time, config
            )
            if result.get("success"):
                return PublicationResult(platform="tiktok", success=True,
                                         post_id=result.get("buffer_id", ""),
                                         scheduled_at=result.get("scheduled_at", ""))
            return PublicationResult(platform="tiktok", success=False, error=result.get("error", "Unknown"))

        except Exception as e:
            logger.error(f"TikTok publish failed: {e}")
            return PublicationResult(platform="tiktok", success=False, error=str(e))

    def validate_credentials(self, config: dict) -> bool:
        token = config.get("apis", {}).get("buffer_access_token", "")
        return bool(token) and not token.startswith("YOUR_")

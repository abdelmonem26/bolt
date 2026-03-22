"""
Instagram adapter -- transform and publish for Instagram Reels.

Pre-plan Section 20:
  - Transform: same master file for Reels, square thumbnail 1080x1080,
    cover frame from second 2 not frame 0
  - Metadata: punchline as standalone caption, up to 30 hashtags in first comment not in caption
  - Publish: Buffer API primary, Instagram Graph API direct as fallback
"""

import logging
import subprocess
from pathlib import Path

import requests

try:
    from . import PlatformAdapter, PlatformPackage, PublicationResult
except ImportError:
    from adapters import PlatformAdapter, PlatformPackage, PublicationResult

logger = logging.getLogger("bolt.dist.instagram")


class InstagramAdapter(PlatformAdapter):
    platform_name = "instagram"

    def transform(self, master_video_path: str, script: dict,
                  article: dict, config: dict) -> PlatformPackage:
        """Instagram Reels: square thumbnail, punchline caption, hashtags for first comment."""
        from caption_composer import compose_caption

        captions = compose_caption(script, article, "instagram", config)

        # Generate 1080x1080 square thumbnail (pre-plan requirement)
        thumb = self._generate_square_thumbnail(article, script.get("content_id", ""), config)

        return PlatformPackage(
            platform="instagram",
            video_path=master_video_path,
            caption=captions["caption"],  # Punchline as standalone caption
            hashtags=captions["hashtags"][:30],  # Up to 30, posted as first comment
            thumbnail_path=thumb or "",
            extra={"hashtags_in_comment": True},  # Signal to post hashtags separately
        )

    def _generate_square_thumbnail(self, article: dict, content_id: str, config: dict) -> str:
        """Generate a 1080x1080 square thumbnail for Instagram."""
        try:
            from PIL import Image, ImageDraw, ImageFont
            out_dir = Path(config.get("paths", {}).get("thumbnails", "/tmp/bolt/thumbnails"))
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"{content_id}_ig_thumb.png"

            img = Image.new("RGBA", (1080, 1080), (0, 71, 171, 255))
            draw = ImageDraw.Draw(img)
            title = article.get("title", "AI News")[:45]
            try:
                font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 52)
            except Exception:
                font = ImageFont.load_default()
            draw.rectangle([40, 800, 1040, 1000], fill=(0, 0, 0, 180))
            draw.text((60, 830), title, fill=(255, 255, 0), font=font)
            draw.text((60, 920), "Bolt AI  Daily AI News", fill=(255, 255, 255), font=font)
            img.save(str(out), "PNG")
            return str(out)
        except Exception as e:
            logger.warning(f"Instagram thumbnail generation failed: {e}")
            return ""

    def publish(self, package: PlatformPackage, config: dict) -> PublicationResult:
        """Publish via Buffer API, fallback to Instagram Graph API."""
        try:
            access_token = config.get("apis", {}).get("buffer_access_token", "")
            if access_token and not access_token.startswith("YOUR_"):
                from buffer_utils import get_buffer_profile_ids, schedule_via_buffer
                profiles = get_buffer_profile_ids(access_token)
                if "instagram" in profiles:
                    post_time = config.get("platforms", {}).get("instagram", {}).get("post_time", "12:00")
                    result = schedule_via_buffer(
                        package.video_path,
                        {"captions": {"instagram": {"caption": package.caption, "hashtags": package.hashtags}},
                         "article": {"title": package.caption[:60]}},
                        "instagram", profiles["instagram"], access_token, post_time, config
                    )
                    if result.get("success"):
                        return PublicationResult(platform="instagram", success=True,
                                                 post_id=result.get("buffer_id", ""),
                                                 scheduled_at=result.get("scheduled_at", ""))

            # Fallback: Direct Instagram Graph API
            return self._publish_direct(package, config)

        except Exception as e:
            logger.error(f"Instagram publish failed: {e}")
            return PublicationResult(platform="instagram", success=False, error=str(e))

    def _publish_direct(self, package: PlatformPackage, config: dict) -> PublicationResult:
        """Direct Instagram Graph API publishing."""
        ig_token = config.get("apis", {}).get("instagram_access_token", "")
        user_id = config.get("apis", {}).get("instagram_user_id", "")
        if not ig_token or ig_token.startswith("YOUR_"):
            return PublicationResult(platform="instagram", success=False, error="No Instagram credentials")

        try:
            import time
            # Step 1: Create media container
            container_resp = requests.post(
                f"https://graph.facebook.com/v19.0/{user_id}/media",
                params={
                    "video_url": package.video_path,
                    "caption": package.caption,
                    "media_type": "REELS",
                    "access_token": ig_token,
                }, timeout=30
            )
            if not container_resp.ok:
                return PublicationResult(platform="instagram", success=False, error=container_resp.text[:200])

            container_id = container_resp.json().get("id")
            if not container_id:
                return PublicationResult(platform="instagram", success=False, error="No container ID")

            # Step 2: Wait and publish
            time.sleep(10)
            publish_resp = requests.post(
                f"https://graph.facebook.com/v19.0/{user_id}/media_publish",
                params={"creation_id": container_id, "access_token": ig_token},
                timeout=20
            )
            if not publish_resp.ok:
                return PublicationResult(platform="instagram", success=False, error=publish_resp.text[:200])

            media_id = publish_resp.json().get("id", "")
            logger.info(f"Instagram Reel published: media_id={media_id}")
            return PublicationResult(platform="instagram", success=True, post_id=media_id)

        except Exception as e:
            return PublicationResult(platform="instagram", success=False, error=str(e))

    def validate_credentials(self, config: dict) -> bool:
        # Either Buffer or direct Instagram creds
        buffer = config.get("apis", {}).get("buffer_access_token", "")
        ig = config.get("apis", {}).get("instagram_access_token", "")
        return (bool(buffer) and not buffer.startswith("YOUR_")) or (bool(ig) and not ig.startswith("YOUR_"))

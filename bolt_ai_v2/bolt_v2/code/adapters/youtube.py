"""
YouTube adapter -- transform and publish for YouTube Shorts.

Pre-plan Section 20:
  - Transform: same master file, 1280x720 thumbnail, title is script hook max 100 chars
  - Metadata: description with script summary + source URL + hashtags, up to 15 tags, category 28
  - Publish: YouTube Data API v3 direct upload, resumable for files over 5MB
"""

import json
import logging
from pathlib import Path

import requests

try:
    from . import PlatformAdapter, PlatformPackage, PublicationResult
except ImportError:
    # Fallback for when code/ is on sys.path (CLI, job_worker entry points)
    from adapters import PlatformAdapter, PlatformPackage, PublicationResult

logger = logging.getLogger("bolt.dist.youtube")


class YouTubeAdapter(PlatformAdapter):
    platform_name = "youtube"

    def transform(self, master_video_path: str, script: dict,
                  article: dict, config: dict) -> PlatformPackage:
        """YouTube Shorts: same master file, title from hook, 1280x720 thumbnail."""
        from caption_composer import compose_caption

        captions = compose_caption(script, article, "youtube", config)

        return PlatformPackage(
            platform="youtube",
            video_path=master_video_path,
            title=captions["title"][:100],
            description=captions["description"],
            hashtags=captions["hashtags"],
            tags=captions.get("tags", ["AI", "artificial intelligence", "tech news"]),
            thumbnail_path=script.get("thumbnail_path", ""),
            extra={"category_id": config.get("platforms", {}).get("youtube", {}).get("category_id", "28")},
        )

    def publish(self, package: PlatformPackage, config: dict) -> PublicationResult:
        """Upload to YouTube via Data API v3."""
        try:
            apis = config.get("apis", {})
            client_id = apis.get("youtube_client_id", "")
            client_secret = apis.get("youtube_client_secret", "")
            refresh_token = apis.get("youtube_refresh_token", "")

            if not client_id or client_id.startswith("YOUR_"):
                return PublicationResult(platform="youtube", success=False, error="YouTube credentials not configured")

            # Step 1: Refresh access token
            token_resp = requests.post("https://oauth2.googleapis.com/token", data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }, timeout=10)
            if not token_resp.ok:
                return PublicationResult(platform="youtube", success=False, error="Token refresh failed")
            access_token = token_resp.json()["access_token"]

            headers = {"Authorization": f"Bearer {access_token}"}

            # Step 2: Upload video
            metadata = {
                "snippet": {
                    "title": f"{package.title} #Shorts",
                    "description": f"{package.description}\n\n{' '.join(package.hashtags)}",
                    "tags": package.tags[:15],
                    "categoryId": package.extra.get("category_id", "28"),
                },
                "status": {
                    "privacyStatus": config.get("platforms", {}).get("youtube", {}).get("privacy", "public"),
                    "selfDeclaredMadeForKids": False,
                },
            }

            upload_url = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=multipart&part=snippet,status"
            from requests_toolbelt import MultipartEncoder

            # Use a with-statement to ensure the file handle is closed even if
            # the upload fails (Bug fix: prevents file handle leak).
            with open(package.video_path, "rb") as video_file:
                mp = MultipartEncoder(fields={
                    "metadata": ("metadata", json.dumps(metadata), "application/json"),
                    "video": ("video.mp4", video_file, "video/mp4"),
                })

                resp = requests.post(upload_url, data=mp,
                                     headers={**headers, "Content-Type": mp.content_type}, timeout=300)
            resp.raise_for_status()
            video_id = resp.json()["id"]
            url = f"https://www.youtube.com/shorts/{video_id}"
            logger.info(f"YouTube Shorts published: {url}")
            return PublicationResult(platform="youtube", success=True, post_url=url, post_id=video_id)

        except Exception as e:
            logger.error(f"YouTube publish failed: {e}")
            return PublicationResult(platform="youtube", success=False, error=str(e))

    def validate_credentials(self, config: dict) -> bool:
        apis = config.get("apis", {})
        key = apis.get("youtube_client_id", "")
        return bool(key) and not key.startswith("YOUR_")

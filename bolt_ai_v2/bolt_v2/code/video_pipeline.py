#!/usr/bin/env python3
"""
Bolt AI — Video Pipeline v3 (100% FREE TOOLS)
Voice: Edge-TTS (unlimited free) → Google Cloud TTS (1M/month free) → ElevenLabs (10K/month free)
Avatar: Vidnoz (free) → D-ID (20 free/month) → FFmpeg text-card fallback
Assembly: FFmpeg + Pillow (open source, completely free)
"""
import asyncio, base64, json, logging, subprocess, time
from datetime import datetime, timezone
from pathlib import Path
import requests

logger = logging.getLogger("bolt.video")

def load_config(path="code/config.json"):
    """Legacy loader -- prefer shared_config.get_config() for secret injection."""
    from shared_config import get_config
    return get_config(path)

# ── VOICE TIER 1: Edge-TTS (unlimited, no key needed) ──
def _run_async(coro):
    """Run an async coroutine safely, whether or not an event loop is already running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # Already inside an event loop (e.g. called from scheduler via async pipeline)
        # Create a new thread to run the coroutine in its own event loop
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=120)
    else:
        return asyncio.run(coro)


def synthesize_edge_tts(script, content_id, config):
    """Use local_tts.py (dedicated edge-tts module) as primary free voice provider."""
    try:
        from local_tts import generate_with_retries
        out_filename = f"{content_id}_edge.mp3"
        result = _run_async(generate_with_retries(script, out_filename, config, max_retries=3))
        return result
    except ImportError:
        pass  # Fallback to inline edge_tts below
    try:
        import edge_tts
        # Read voice settings from config if set, else use defaults from local_tts
        tts_cfg = config.get("local_tts", {})
        voice  = tts_cfg.get("voice",  "en-US-GuyNeural")
        rate   = tts_cfg.get("rate",   "+8%")
        pitch  = tts_cfg.get("pitch",  "+5Hz")
        out_dir = Path(config["paths"]["audio"]); out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{content_id}_edge.mp3"
        async def _run():
            c = edge_tts.Communicate(script, voice=voice, rate=rate, pitch=pitch)
            await c.save(str(out_path))
        _run_async(_run())
        logger.info(f"Edge TTS OK: {out_path.name}")
        return str(out_path)
    except ImportError:
        logger.info("edge-tts missing — pip install edge-tts")
        return None
    except Exception as e:
        logger.warning(f"Edge TTS error: {e}"); return None

# ── VOICE TIER 2: Google Cloud TTS (1M chars/month free) ──
def synthesize_google_tts(script, content_id, config):
    key = config["apis"].get("google_cloud_tts_key","")
    if not key or key.startswith("YOUR_"): return None
    try:
        resp = requests.post(
            f"https://texttospeech.googleapis.com/v1/text:synthesize?key={key}",
            json={"input":{"text":script},"voice":{"languageCode":"en-US","name":"en-US-Neural2-J","ssmlGender":"MALE"},
                  "audioConfig":{"audioEncoding":"MP3","speakingRate":1.08,"pitch":1.5,"volumeGainDb":2.0}},
            timeout=20)
        resp.raise_for_status()
        audio = base64.b64decode(resp.json()["audioContent"])
        out_dir = Path(config["paths"]["audio"]); out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{content_id}_gcp.mp3"
        out_path.write_bytes(audio)
        logger.info(f"Google TTS OK: {out_path.name} ({len(audio)//1024}KB)"); return str(out_path)
    except Exception as e:
        logger.warning(f"Google TTS error: {e}"); return None

# ── VOICE TIER 3: ElevenLabs (10K chars/month free) ──
def synthesize_elevenlabs(script, content_id, config):
    key = config["apis"].get("elevenlabs_api_key","")
    vid = config["apis"].get("elevenlabs_voice_id","")
    if not key or key.startswith("YOUR_") or not vid or vid.startswith("YOUR_"): return None
    try:
        resp = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
            headers={"Accept":"audio/mpeg","Content-Type":"application/json","xi-api-key":key},
            json={"text":script,"model_id":"eleven_turbo_v2",
                  "voice_settings":{"stability":0.42,"similarity_boost":0.85,"style":0.30}}, timeout=30)
        if resp.status_code == 429: logger.warning("ElevenLabs quota exhausted"); return None
        resp.raise_for_status()
        out_dir = Path(config["paths"]["audio"]); out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{content_id}_el.mp3"
        out_path.write_bytes(resp.content)
        logger.info(f"ElevenLabs OK: {out_path.name}"); return str(out_path)
    except Exception as e:
        logger.warning(f"ElevenLabs error: {e}"); return None

def synthesize_voice(script, content_id, config):
    """Try providers in order — all free. Returns (path, provider_name) or (None, None)."""
    for provider, fn in [("edge_tts", lambda: synthesize_edge_tts(script, content_id, config)),
                         ("google_tts", lambda: synthesize_google_tts(script, content_id, config)),
                         ("elevenlabs", lambda: synthesize_elevenlabs(script, content_id, config))]:
        logger.info(f"Trying: {provider}")
        r = fn()
        if r: return r, provider
    logger.error("All voice providers failed"); return None, None

# ── AVATAR TIER 1: Vidnoz (free, 1900+ avatars) ──
def create_vidnoz_video(audio_path, content_id, config):
    key = config["apis"].get("vidnoz_api_key","")
    avatar_id = config["apis"].get("vidnoz_avatar_id","")
    if not key or key.startswith("YOUR_"): return None
    try:
        with open(audio_path,"rb") as f:
            up = requests.post("https://app.vidnoz.com/api/v1/audio/upload",
                headers={"api-key":key}, files={"audio":(Path(audio_path).name,f,"audio/mpeg")}, timeout=30)
        up.raise_for_status()
        audio_url = up.json().get("data",{}).get("url","")
        if not audio_url: return None
        gen = requests.post("https://app.vidnoz.com/api/v1/video/generate",
            headers={"api-key":key,"Content-Type":"application/json"},
            json={"avatar_id":avatar_id,"audio_url":audio_url,"resolution":"1080x1920","background":"#0047AB"},
            timeout=30)
        gen.raise_for_status()
        task_id = gen.json().get("data",{}).get("task_id","")
        if not task_id: return None
        deadline = time.time()+300
        while time.time()<deadline:
            r = requests.get(f"https://app.vidnoz.com/api/v1/video/status/{task_id}",headers={"api-key":key},timeout=10)
            data = r.json().get("data",{})
            logger.info(f"Vidnoz: {data.get('status')}")
            if data.get("status")=="completed":
                return _download(data.get("video_url",""), content_id, config, "_vidnoz")
            if data.get("status")=="failed": return None
            time.sleep(8)
    except Exception as e:
        logger.warning(f"Vidnoz error: {e}"); return None

# ── AVATAR TIER 2: D-ID (20 free credits/month) ──
def create_did_video(audio_path, content_id, config):
    key = config["apis"].get("did_api_key","")
    presenter = config["apis"].get("did_presenter_url","")
    if not key or key.startswith("YOUR_"): return None
    h = {"Authorization":f"Basic {key}","Content-Type":"application/json"}
    try:
        with open(audio_path,"rb") as f:
            up = requests.post("https://api.d-id.com/audios",
                headers={"Authorization":f"Basic {key}"}, files={"audio":(Path(audio_path).name,f,"audio/mpeg")}, timeout=30)
        up.raise_for_status()
        audio_url = up.json().get("url","")
        talk = requests.post("https://api.d-id.com/talks", headers=h,
            json={"source_url":presenter,"script":{"type":"audio","audio_url":audio_url},
                  "config":{"result_format":"mp4","fluent":True}}, timeout=30)
        talk.raise_for_status()
        talk_id = talk.json().get("id","")
        if not talk_id: return None
        deadline = time.time()+300
        while time.time()<deadline:
            r = requests.get(f"https://api.d-id.com/talks/{talk_id}", headers=h, timeout=10).json()
            logger.info(f"D-ID: {r.get('status')}")
            if r.get("status")=="done": return _download(r.get("result_url",""), content_id, config, "_did")
            if r.get("status")=="error": return None
            time.sleep(6)
    except Exception as e:
        logger.warning(f"D-ID error: {e}"); return None

def _download(url, content_id, config, suffix):
    if not url: return None
    try:
        out_dir = Path(config["paths"]["video"]); out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{content_id}{suffix}.mp4"
        r = requests.get(url, stream=True, timeout=120); r.raise_for_status()
        with open(out,"wb") as f:
            for chunk in r.iter_content(8192): f.write(chunk)
        logger.info(f"Downloaded: {out.name}"); return str(out)
    except Exception as e:
        logger.error(f"Download failed: {e}"); return None

# ── ASSEMBLY: FFmpeg (completely free, open source) ──
def assemble_ffmpeg(audio_path, avatar_path, content_id, script, config):
    out_dir = Path(config["paths"]["video"]); out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"{content_id}_final.mp4"
    assets = Path(config.get("paths",{}).get("assets","assets"))
    logo = assets / "bolt_logo.png"

    if avatar_path and Path(avatar_path).exists():
        logo_filter = f";[1:v]scale=60:-1[lg];[base][lg]overlay=W-w-20:20[out]" if logo.exists() else ""
        logo_in = ["-i", str(logo)] if logo.exists() else []
        cmd = ["ffmpeg","-y","-i",avatar_path,*logo_in,"-i",audio_path,
               "-filter_complex",f"[0:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=#0047AB[base]{logo_filter}",
               "-map","[out]" if logo.exists() else "[base]","-map","2:a" if logo.exists() else "1:a",
               "-c:v","libx264","-preset","fast","-crf","23","-c:a","aac","-b:a","192k","-shortest","-movflags","+faststart",str(final)]
    else:
        headline = script.split(".")[0][:55].replace("'","\\'")
        cmd = ["ffmpeg","-y","-f","lavfi","-i","color=c=0x0047AB:size=1080x1920:rate=30",
               "-i",audio_path,"-filter_complex",
               f"[0:v]drawtext=text='{headline}':fontcolor=yellow:fontsize=50:x=(w-tw)/2:y=(h-th)/2[v]",
               "-map","[v]","-map","1:a","-c:v","libx264","-preset","fast","-crf","23",
               "-c:a","aac","-b:a","192k","-shortest","-movflags","+faststart",str(final)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode==0: logger.info(f"FFmpeg OK: {final.name}"); return str(final)
        else: logger.error(f"FFmpeg error: {r.stderr[-300:]}"); return None
    except FileNotFoundError:
        logger.warning("FFmpeg not installed. sudo apt install ffmpeg"); return None
    except Exception as e:
        logger.error(f"FFmpeg error: {e}"); return None

# ── THUMBNAIL: Pillow (completely free) ──
def generate_thumbnail(article, content_id, config):
    try:
        from PIL import Image, ImageDraw, ImageFont
        assets = Path(config.get("paths",{}).get("assets","assets"))
        out_dir = Path(config["paths"]["thumbnails"]); out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{content_id}_thumb.png"
        tmpl = assets / "thumbnail_template.png"
        img = Image.open(tmpl).convert("RGBA") if tmpl.exists() else Image.new("RGBA",(1280,720),(0,71,171,255))
        draw = ImageDraw.Draw(img)
        title = article.get("title","AI News Update")[:55]
        try: font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",60)
        except: font = ImageFont.load_default()
        draw.rectangle([40,480,1240,680],fill=(0,0,0,180))
        draw.text((60,500),title,fill=(255,255,0),font=font)
        img.save(str(out),"PNG")
        logger.info(f"Thumbnail OK: {out.name}"); return str(out)
    except ImportError:
        logger.info("Pillow not installed. pip install Pillow"); return None
    except Exception as e:
        logger.warning(f"Thumbnail error: {e}"); return None

# ── MAIN ORCHESTRATOR ──
def run_video_pipeline(package, config):
    """
    Run the full video pipeline with incremental DB writes.

    After each sub-step (audio, avatar, assembly), status is written
    to the DB immediately so crash recovery can resume from the last
    successful sub-step. This follows the pre-plan Pattern 4:
    "State is always in the DB, never in memory."
    """
    cid, script, article = package["content_id"], package["script"], package["article"]
    logger.info(f"Free video pipeline: {cid}")

    # Get DB for incremental status writes
    try:
        from database import get_db
        db = get_db()
        db.ensure_video_row(cid)
    except Exception:
        db = None  # Graceful degradation -- still works without incremental writes

    # Check if we can resume from a previous partial run
    existing = db.get_video_status(cid) if db else None
    if existing and existing.get("status") in ("audio_ready", "avatar_ready"):
        logger.info(f"Resuming video pipeline from status={existing['status']}", extra={"content_id": cid})

    # ── Sub-step 1: Voice synthesis ──
    if (existing and existing.get("audio_path")
            and existing["status"] in ("audio_ready", "avatar_ready")
            and Path(existing["audio_path"]).exists()):
        audio = existing["audio_path"]
        audio_provider = existing.get("audio_provider", "edge_tts")
        logger.info(f"Skipping voice synthesis -- resuming with existing audio", extra={"content_id": cid})
    else:
        audio, audio_provider = synthesize_voice(script, cid, config)
        if not audio:
            package.update({"status": "failed", "error": "All free TTS providers failed"})
            if db:
                db.update_video_status(cid, "failed")
            return package
        # Incremental write: audio done
        if db:
            db.update_video_status(cid, "audio_ready", audio_path=audio, audio_provider=audio_provider)
            logger.info("Video status -> audio_ready", extra={"content_id": cid})

    package["audio_path"] = audio
    package["audio_provider"] = audio_provider

    # ── Sub-step 2: Avatar generation ──
    if (existing and existing.get("avatar_path")
            and existing["status"] == "avatar_ready"
            and Path(existing["avatar_path"]).exists()):
        avatar = existing["avatar_path"]
        avatar_provider = existing.get("avatar_provider")
        logger.info(f"Skipping avatar generation -- resuming with existing avatar", extra={"content_id": cid})
    else:
        avatar = create_vidnoz_video(audio, cid, config)
        avatar_provider = "vidnoz" if avatar else None
        if not avatar:
            avatar = create_did_video(audio, cid, config)
            avatar_provider = "did" if avatar else None
        if not avatar:
            logger.warning("No avatar available -- using text-card fallback")
        # Incremental write: avatar done (or skipped to text-card)
        if db:
            db.update_video_status(
                cid, "avatar_ready",
                avatar_path=avatar or "", avatar_provider=avatar_provider or "ffmpeg_fallback",
            )
            logger.info("Video status -> avatar_ready", extra={"content_id": cid})

    package["avatar_video_path"] = avatar
    package["avatar_provider"] = avatar_provider

    # ── Sub-step 3: Assembly ──
    final = assemble_ffmpeg(audio, avatar, cid, script, config)
    package["final_video_path"] = final
    package["video_ready"] = final is not None
    package["thumbnail_path"] = generate_thumbnail(article, cid, config)
    package["status"] = "ready_to_publish" if final else ("audio_only" if audio else "failed")
    package["video_completed_at"] = datetime.now(timezone.utc).isoformat()

    # Incremental write: assembled (or failed)
    if db:
        final_status = "assembled" if final else "failed"
        db.update_video_status(
            cid, final_status,
            final_path=final or "", thumbnail_path=package.get("thumbnail_path") or "",
            video_ready=1 if final else 0,
        )
        logger.info(f"Video status -> {final_status}", extra={"content_id": cid})

    logger.info(f"Pipeline done: {package['status']}"); return package

def run(config: dict | None = None, *, config_path: str = "code/config.json"):
    """Run the video pipeline for the next approved script in the queue.

    Args:
        config: Pre-loaded config dict (preferred). When provided, config_path is ignored.
        config_path: Legacy fallback -- used only when config is None (CLI usage).
    """
    if config is None:
        config = load_config(config_path)
    queue_dir = Path(config["paths"]["queue"])
    scripts = sorted(queue_dir.glob("script_*.json"))
    if not scripts: logger.warning("No scripts in queue"); return None
    pkg = json.loads(scripts[0].read_text())
    if pkg.get("status") not in ("approved","pending_review"): return None
    result = run_video_pipeline(pkg, config)
    scripts[0].write_text(json.dumps(result, indent=2))
    return result

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    r = run()
    if r: print(f"\nStatus: {r['status']}\nAudio: {r.get('audio_path')}\nVideo: {r.get('final_video_path')}")


# ════════════════════════════════════════════════════════════════════
# RESTORED FUNCTIONS — migrated from original VideoCreationPipeline class
# ════════════════════════════════════════════════════════════════════

def create_captions(script: str, content_id: str, config: dict) -> str | None:
    """
    Generate an SRT subtitle file from the script.
    Replaces VideoCreationPipeline.create_captions() from v1.
    Estimates timing at ~2.5 words per second (Bolt's speaking rate).
    Returns path to .srt file or None.
    """
    out_dir = Path(config["paths"]["video"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{content_id}.srt"

    words     = script.split()
    wps       = 2.5      # words per second at Bolt's pace
    chunk_size = 7       # words per subtitle line

    lines = []
    idx   = 1
    for i in range(0, len(words), chunk_size):
        chunk     = words[i:i + chunk_size]
        start_sec = i / wps
        end_sec   = (i + len(chunk)) / wps
        lines.append(
            f"{idx}\n"
            f"{_seconds_to_srt_time(start_sec)} --> {_seconds_to_srt_time(end_sec)}\n"
            f"{' '.join(chunk)}\n"
        )
        idx += 1

    out_path.write_text("\n".join(lines))
    logger.info(f"Captions generated: {out_path.name} ({idx-1} entries)")
    return str(out_path)


def _seconds_to_srt_time(seconds: float) -> str:
    """
    Convert seconds to SRT timestamp format HH:MM:SS,mmm.
    Replaces VideoCreationPipeline._seconds_to_srt_time() from v1.
    """
    h   = int(seconds // 3600)
    m   = int((seconds % 3600) // 60)
    s   = int(seconds % 60)
    ms  = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def create_platform_thumbnail(article: dict, content_id: str,
                               platform: str, config: dict) -> str | None:
    """
    Create a platform-specific branded thumbnail.
    Replaces VideoCreationPipeline.create_thumbnail() + platform overrides from v1.
    Supports youtube / tiktok / instagram with different aspect ratios and styles.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.warning("Pillow not installed — pip install Pillow")
        return None

    assets  = Path(config.get("paths", {}).get("assets", "assets"))
    out_dir = Path(config["paths"]["thumbnails"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{content_id}_{platform}_thumb.png"

    # Platform-specific dimensions
    sizes = {
        "youtube":   (1280, 720),    # 16:9
        "tiktok":    (1080, 1920),   # 9:16
        "instagram": (1080, 1080),   # 1:1
    }
    w, h = sizes.get(platform, (1280, 720))

    # Base image
    tmpl = assets / "thumbnail_template.png"
    if tmpl.exists():
        base = Image.open(tmpl).convert("RGBA").resize((w, h))
    else:
        base = Image.new("RGBA", (w, h), (0, 71, 171, 255))  # Bolt brand blue

    _add_gradient_background(base)
    _add_bolt_branding(base, assets)
    _add_title_text(base, article.get("title", "AI Update")[:50])

    # Platform-specific overlays
    if platform == "youtube":
        _add_youtube_elements(base, article)
    elif platform == "tiktok":
        _add_tiktok_elements(base, article)
    elif platform == "instagram":
        _add_instagram_elements(base, article)

    base.save(str(out_path), "PNG")
    logger.info(f"Thumbnail generated [{platform}]: {out_path.name}")
    return str(out_path)


def _add_gradient_background(img: Image.Image) -> None:
    """Add a subtle gradient overlay. Replaces v1 method."""
    try:
        from PIL import ImageDraw
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw    = ImageDraw.Draw(overlay)
        for i in range(img.height // 2, img.height):
            alpha = int((i - img.height // 2) / (img.height // 2) * 160)
            draw.line([(0, i), (img.width, i)], fill=(0, 0, 0, alpha))
        img.alpha_composite(overlay)
    except Exception as e:
        logger.debug(f"_add_gradient_background: {e}")


def _add_bolt_branding(img: Image.Image, assets_path: Path) -> None:
    """Overlay Bolt logo watermark. Replaces v1 method."""
    try:
        logo_path = assets_path / "bolt_logo.png"
        if not logo_path.exists():
            return
        logo = Image.open(logo_path).convert("RGBA")
        logo.thumbnail((80, 80))
        x = img.width - logo.width - 20
        y = 20
        img.paste(logo, (x, y), logo)
    except Exception as e:
        logger.debug(f"_add_bolt_branding: {e}")


def _add_title_text(img: Image.Image, title: str) -> None:
    """Add title text to the lower portion of the thumbnail. Replaces v1 method."""
    try:
        from PIL import ImageDraw, ImageFont
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 52)
        except Exception:
            font = ImageFont.load_default()
        y_pos  = img.height - 180
        draw.rectangle([0, y_pos - 20, img.width, img.height], fill=(0, 0, 0, 180))
        draw.text((40, y_pos + 10), title, fill=(255, 255, 0), font=font)
    except Exception as e:
        logger.debug(f"_add_title_text: {e}")


def _add_youtube_elements(img: Image.Image, article: dict) -> None:
    """Add YouTube-specific overlay (play button, source tag). Replaces v1 method."""
    try:
        from PIL import ImageDraw, ImageFont
        draw = ImageDraw.Draw(img)
        # Source tag top-left
        try:
            font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
        except Exception:
            font_sm = ImageFont.load_default()
        source = article.get("source", "AI News")[:25]
        draw.rectangle([20, 20, len(source)*17 + 40, 58], fill=(255, 255, 0, 220))
        draw.text((30, 26), source, fill=(0, 0, 0), font=font_sm)
    except Exception as e:
        logger.debug(f"_add_youtube_elements: {e}")


def _add_tiktok_elements(img: Image.Image, article: dict) -> None:
    """Add TikTok-specific overlay (vertical format badge). Replaces v1 method."""
    try:
        from PIL import ImageDraw, ImageFont
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
        except Exception:
            font = ImageFont.load_default()
        draw.text((40, img.height - 300), "⚡ Follow @BoltAI", fill=(255, 255, 0), font=font)
    except Exception as e:
        logger.debug(f"_add_tiktok_elements: {e}")


def _add_instagram_elements(img: Image.Image, article: dict) -> None:
    """Add Instagram-specific overlay (square format, bold branding). Replaces v1 method."""
    try:
        from PIL import ImageDraw, ImageFont
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 40)
        except Exception:
            font = ImageFont.load_default()
        draw.text((40, img.height - 120), "Bolt AI ⚡ Daily AI News", fill=(255, 255, 255), font=font)
    except Exception as e:
        logger.debug(f"_add_instagram_elements: {e}")


def create_animated_background(duration: int, content_id: str, config: dict) -> str | None:
    """
    Create an animated tech-style background video using FFmpeg.
    Replaces VideoCreationPipeline.create_animated_background() +
    _create_tech_animation() + _add_circuit_pattern() etc. from v1.

    Generates a {duration}s MP4 with animated grid overlay on Bolt brand blue.
    Falls back gracefully if FFmpeg is unavailable.
    """
    out_dir = Path(config["paths"]["video"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{content_id}_background.mp4"

    # FFmpeg complex filter: animated grid + pulsing glow on brand blue background
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", (
            f"color=c=0x0047AB:size=1080x1920:rate=30,"
            f"drawgrid=width=54:height=54:thickness=1:color=0xFFFF0020,"
            f"eq=brightness=0.02*sin(2*PI*t/2)"  # Subtle pulsing brightness
        ),
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "fast", "-crf", "28",
        "-movflags", "+faststart",
        str(out_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            logger.info(f"Animated background generated: {out_path.name}")
            return str(out_path)
        logger.warning(f"FFmpeg background failed: {result.stderr[-200:]}")
        return None
    except FileNotFoundError:
        logger.debug("FFmpeg not available for animated background")
        return None
    except Exception as e:
        logger.warning(f"create_animated_background: {e}")
        return None


def _get_platform_settings(platform: str, config: dict) -> dict:
    """
    Get platform-specific video settings.
    Replaces VideoCreationPipeline._get_platform_settings() from v1.
    """
    defaults = {
        "youtube":   {"width": 1080, "height": 1920, "fps": 30, "max_duration": 60,  "format": "mp4"},
        "tiktok":    {"width": 1080, "height": 1920, "fps": 30, "max_duration": 60,  "format": "mp4"},
        "instagram": {"width": 1080, "height": 1920, "fps": 30, "max_duration": 90,  "format": "mp4"},
    }
    return defaults.get(platform, defaults["youtube"])

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from urllib.parse import urlparse
from youtube_transcript_api import YouTubeTranscriptApi
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from cachetools import TTLCache
import re
import os
import shutil
import subprocess
import tempfile
import time
import logging
import requests
import yt_dlp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("api")

# -----------------------------
# Rate limiting (in-memory — fine for a single process; if this ever
# runs as multiple replicas behind a load balancer, swap the storage_uri
# for a shared Redis backend so limits are enforced across instances)
# -----------------------------
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="YouTube Transcript API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# -----------------------------
# CORS
# -----------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------
# Request logging
#
# No third-party observability tool wired up yet — this at least makes
# traffic and errors visible in the server's own logs.
# -----------------------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration_ms = round((time.time() - start) * 1000)
    logger.info(
        f'{request.client.host if request.client else "-"} '
        f'"{request.method} {request.url.path}" '
        f"{response.status_code} {duration_ms}ms"
    )
    return response


# -----------------------------
# Request Model
# -----------------------------
class VideoRequest(BaseModel):
    url: str


# -----------------------------
# Extract Video ID
# -----------------------------
def extract_video_id(url: str):
    patterns = [
        r"(?:v=|\/)([0-9A-Za-z_-]{11}).*",
        r"youtu\.be\/([0-9A-Za-z_-]{11})",
        r"youtube\.com\/shorts\/([0-9A-Za-z_-]{11})",
        r"youtube\.com\/embed\/([0-9A-Za-z_-]{11})",
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    return None


# -----------------------------
# oEmbed lookup (title/channel/thumbnail, no API key needed)
# -----------------------------
def get_oembed(video_id: str):
    try:
        res = requests.get(
            "https://www.youtube.com/oembed",
            params={
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "format": "json",
            },
            timeout=5,
        )
        if res.status_code == 200:
            data = res.json()
            return {
                "title": data.get("title"),
                "channel": data.get("author_name"),
            }
    except Exception:
        pass

    return {"title": None, "channel": None}


# -----------------------------
# Home
# -----------------------------
@app.get("/")
def home():
    return {
        "success": True,
        "message": "YouTube Transcript API Running 🚀"
    }


@app.get("/hello")
def hello():
    return {
        "message": "Backend Working 🚀"
    }


# -----------------------------
# Transcript Endpoint
#
# Cached by video_id — a popular video gets hit by many people, and the
# transcript itself doesn't change, so there's no reason to re-fetch it
# from YouTube every time.
# -----------------------------
transcript_cache = TTLCache(maxsize=1000, ttl=3600)


@app.post("/transcript")
@limiter.limit("20/minute")
def transcript(request: Request, data: VideoRequest):

    video_id = extract_video_id(data.url)

    if not video_id:
        return {
            "success": False,
            "message": "Invalid YouTube URL"
        }

    if video_id in transcript_cache:
        return transcript_cache[video_id]

    try:

        api = YouTubeTranscriptApi()

        transcript_list = api.list(video_id)

        available_languages = []

        for t in transcript_list:
            available_languages.append({
                "language": t.language,
                "language_code": t.language_code,
                "generated": t.is_generated
            })

        # Prefer manually created transcript
        try:
            transcript_obj = transcript_list.find_manually_created_transcript(
                [t["language_code"] for t in available_languages]
            )

        except Exception:
            transcript_obj = transcript_list.find_generated_transcript(
                [t["language_code"] for t in available_languages]
            )

        transcript = transcript_obj.fetch()
        raw = transcript.to_raw_data()

        duration = 0
        if raw:
            last = raw[-1]
            duration = round(last.get("start", 0) + last.get("duration", 0))

        oembed = get_oembed(video_id)

        result = {
            "success": True,
            "video_id": video_id,
            "title": oembed["title"],
            "channel": oembed["channel"],
            "duration": duration,
            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
            "selected_language": transcript_obj.language,
            "selected_language_code": transcript_obj.language_code,
            "generated": transcript_obj.is_generated,
            "available_languages": available_languages,
            "transcript": raw
        }

        transcript_cache[video_id] = result
        return result

    except Exception as e:
        return {
            "success": False,
            "message": str(e)
        }


# -----------------------------
# YouTube video downloader
#
# YouTube's generic "bestvideo" selector reaches for whatever is
# technically the highest-quality adaptive stream, which increasingly
# requires solving a JS "n challenge" we don't have a solver for (deno)
# — that fails outright. Requesting a *specific* format_id instead
# (e.g. "298" for 720p) reliably works even for adaptive streams, video
# alone or merged with a specific audio format_id via ffmpeg — verified
# directly. So: at /youtube/info time we inspect the real formats
# available for this exact video and offer only what's actually there.
# -----------------------------
MAX_VIDEO_DURATION_SECONDS = 60 * 60  # 1 hour — protects the server from
# someone pasting a multi-hour stream and tying up disk/bandwidth on it.

PREFERRED_HEIGHTS = [1080, 720, 480, 360, 240]

youtube_info_cache = TTLCache(maxsize=500, ttl=600)


def get_format_lists(url: str):
    """Returns (info, formats) from a single extract_info call, reused by
    both /youtube/info and /youtube/download so they always agree on
    what's actually available for this video."""
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return info, info.get("formats", [])


def pick_video_qualities(formats):
    """One best format_id per preferred height, preferring mp4/avc1 for
    broad player compatibility over webm/av1."""
    by_height = {}
    for f in formats:
        height = f.get("height")
        if not height or height not in PREFERRED_HEIGHTS:
            continue
        if f.get("vcodec") == "none":
            continue
        is_mp4 = f.get("ext") == "mp4"
        existing = by_height.get(height)
        if existing is None or (is_mp4 and existing["ext"] != "mp4"):
            by_height[height] = {"format_id": f["format_id"], "ext": f.get("ext")}

    return [
        {"label": f"{h}p", "format_id": by_height[h]["format_id"]}
        for h in PREFERRED_HEIGHTS
        if h in by_height
    ]


def pick_best_audio(formats):
    """Picks a standard ~128kbps m4a stream over exotic high-bitrate ones —
    verified those ultra-high-bitrate audio formats (~380kbps+) 403 even
    when the video streams at the same tier work fine, likely gated
    behind an entitlement check we don't have. 128kbps m4a is the
    universally-available, reliably-accessible tier."""
    audio_formats = [
        f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"
    ]
    if not audio_formats:
        return None

    m4a = [f for f in audio_formats if f.get("ext") == "m4a" and (f.get("abr") or 0) <= 160]
    pool = m4a or [f for f in audio_formats if (f.get("abr") or 0) <= 160] or audio_formats

    pool.sort(key=lambda f: f.get("abr") or 0, reverse=True)
    return pool[0]["format_id"]


@app.post("/youtube/info")
@limiter.limit("15/minute")
def youtube_info(request: Request, data: VideoRequest):
    video_id = extract_video_id(data.url)

    if not video_id:
        return {"success": False, "message": "Invalid YouTube URL"}

    if video_id in youtube_info_cache:
        return youtube_info_cache[video_id]

    try:
        info, formats = get_format_lists(data.url)

        duration = info.get("duration") or 0

        if duration and duration > MAX_VIDEO_DURATION_SECONDS:
            return {
                "success": False,
                "message": "This video is too long to download (over 1 hour). Try a shorter video.",
            }

        qualities = pick_video_qualities(formats)
        has_audio = pick_best_audio(formats) is not None

        if not qualities:
            # No adaptive formats matched our preferred heights — fall back
            # to whatever progressive (audio+video already combined) format
            # exists, which is nearly always available.
            for f in formats:
                if f.get("vcodec") != "none" and f.get("acodec") != "none":
                    qualities = [{"label": f'{f.get("height") or "Standard"}p', "format_id": f["format_id"]}]
                    break

        result = {
            "success": True,
            "video_id": video_id,
            "title": info.get("title"),
            "channel": info.get("uploader") or info.get("channel"),
            "duration": duration,
            "thumbnail": info.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
            "qualities": qualities,
            "has_audio_option": has_audio,
        }

        youtube_info_cache[video_id] = result
        return result

    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/youtube/download")
@limiter.limit("5/minute")
def youtube_download(request: Request, url: str, format_id: str = "", filename: str = "video.mp4"):
    video_id = extract_video_id(url)

    if not video_id:
        return {"success": False, "message": "Invalid YouTube URL"}

    safe_name = re.sub(r'[\\/*?:"<>|]', "_", filename)
    tmp_dir = tempfile.mkdtemp(prefix="ytdl_")

    try:
        info, formats = get_format_lists(url)

        duration = info.get("duration") or 0
        if duration and duration > MAX_VIDEO_DURATION_SECONDS:
            return {
                "success": False,
                "message": "This video is too long to download (over 1 hour).",
            }

        if format_id == "audio":
            audio_id = pick_best_audio(formats)
            if not audio_id:
                return {"success": False, "message": "No audio stream available for this video"}

            if shutil.which("ffmpeg") is None:
                return {
                    "success": False,
                    "message": "ffmpeg is not installed on the server, so audio extraction is unavailable.",
                }

            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "format": audio_id,
                "outtmpl": os.path.join(tmp_dir, "audio_src.%(ext)s"),
                "noplaylist": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)

            src_files = [f for f in os.listdir(tmp_dir) if f.startswith("audio_src")]
            if not src_files:
                return {"success": False, "message": "Audio download failed"}

            src_path = os.path.join(tmp_dir, src_files[0])
            mp3_path = os.path.join(tmp_dir, "output.mp3")

            result = subprocess.run(
                ["ffmpeg", "-y", "-i", src_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", mp3_path],
                capture_output=True,
                timeout=120,
            )
            if result.returncode != 0 or not os.path.exists(mp3_path):
                return {"success": False, "message": "Audio conversion failed"}

            with open(mp3_path, "rb") as f:
                audio_bytes = f.read()

            mp3_name = re.sub(r"\.mp4$", ".mp3", safe_name, flags=re.IGNORECASE)
            if not mp3_name.lower().endswith(".mp3"):
                mp3_name += ".mp3"

            return StreamingResponse(
                iter([audio_bytes]),
                media_type="audio/mpeg",
                headers={"Content-Disposition": f'attachment; filename="{mp3_name}"'},
            )

        # Video path: use the requested format_id if it's still valid for
        # this video, otherwise fall back to the best available quality.
        valid_ids = {f["format_id"] for f in formats}
        chosen_format = format_id if format_id in valid_ids else None

        if not chosen_format:
            qualities = pick_video_qualities(formats)
            if qualities:
                chosen_format = qualities[0]["format_id"]

        if not chosen_format:
            for f in formats:
                if f.get("vcodec") != "none" and f.get("acodec") != "none":
                    chosen_format = f["format_id"]
                    break

        if not chosen_format:
            return {"success": False, "message": "No downloadable format found for this video"}

        # If the chosen format is video-only, merge with the best audio.
        chosen_meta = next((f for f in formats if f["format_id"] == chosen_format), None)
        needs_audio = chosen_meta and chosen_meta.get("acodec") == "none"

        format_selector = chosen_format
        if needs_audio:
            audio_id = pick_best_audio(formats)
            if audio_id:
                format_selector = f"{chosen_format}+{audio_id}"

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "format": format_selector,
            "merge_output_format": "mp4",
            "outtmpl": os.path.join(tmp_dir, "video.%(ext)s"),
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        files = [f for f in os.listdir(tmp_dir) if f.startswith("video")]
        if not files:
            return {"success": False, "message": "Download failed"}

        file_path = os.path.join(tmp_dir, files[0])

        with open(file_path, "rb") as f:
            video_bytes = f.read()

        return StreamingResponse(
            iter([video_bytes]),
            media_type="video/mp4",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
        )

    except Exception as e:
        return {"success": False, "message": str(e)}

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# -----------------------------
# Pinterest
# -----------------------------
class MediaRequest(BaseModel):
    url: str


def extract_pinterest_pin_id(url: str):
    match = re.search(r"pinterest\.[a-z.]+/pin/(\d+)", url)
    if match:
        return match.group(1)

    if "pin.it" in url:
        try:
            res = requests.head(
                url,
                allow_redirects=True,
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            match = re.search(r"/pin/(\d+)", res.url)
            if match:
                return match.group(1)
        except Exception:
            pass

    return None


def find_first_mp4(node):
    """Recursively walk Pinterest's pin JSON (regular pins and story/idea
    pins use different nesting) and return the first .mp4 url found."""
    if isinstance(node, dict):
        url = node.get("url")
        if isinstance(url, str) and ".mp4" in url:
            return url
        for value in node.values():
            found = find_first_mp4(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = find_first_mp4(item)
            if found:
                return found
    return None


# Shorter TTL than YouTube — Pinterest's CDN URLs (pinimg.com/v1.pinimg.com)
# can rotate, so we don't want to hand out a stale link for too long.
pinterest_cache = TTLCache(maxsize=1000, ttl=600)


@app.post("/pinterest/download")
@limiter.limit("20/minute")
def pinterest_download(request: Request, data: MediaRequest):
    pin_id = extract_pinterest_pin_id(data.url)

    if not pin_id:
        return {"success": False, "message": "Invalid Pinterest URL"}

    if pin_id in pinterest_cache:
        return pinterest_cache[pin_id]

    try:
        res = requests.get(
            "https://widgets.pinterest.com/v3/pidgets/pins/info/",
            params={"pin_ids": pin_id},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        payload = res.json()
        pin = payload.get("data", [{}])[0]

        if pin.get("error"):
            return {"success": False, "message": "Pin not found or is private"}

        thumbnail = None
        images = pin.get("images") or {}
        for key in ["736x", "564x", "237x", "236x"]:
            if key in images:
                thumbnail = images[key].get("url")
                break

        video_url = find_first_mp4(pin)

        result = {
            "success": True,
            "pin_id": pin_id,
            "title": pin.get("description") or pin.get("title") or "Pinterest Pin",
            "thumbnail": thumbnail,
            "is_video": video_url is not None,
            "video": video_url,
            "image": thumbnail,
        }

        pinterest_cache[pin_id] = result
        return result

    except Exception as e:
        return {"success": False, "message": str(e)}


# -----------------------------
# Download proxy
#
# Pinterest's CDN URLs are cross-origin, so a plain <a download> won't
# reliably force a save — browsers just navigate to them instead.
# Streaming the bytes through our own server with a Content-Disposition
# header guarantees a real download. Restricted to known media CDNs only,
# so this can't be abused as an open SSRF proxy.
# -----------------------------
ALLOWED_DOWNLOAD_HOSTS = (
    "pinimg.com",
)


@app.get("/download-file")
@limiter.limit("30/minute")
def download_file(request: Request, url: str, filename: str = "download"):
    host = urlparse(url).hostname or ""

    if not any(host == h or host.endswith("." + h) for h in ALLOWED_DOWNLOAD_HOSTS):
        return {"success": False, "message": "This host is not allowed"}

    try:
        upstream = requests.get(
            url,
            stream=True,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )

        safe_name = re.sub(r'[\\/*?:"<>|]', "_", filename)

        return StreamingResponse(
            upstream.iter_content(chunk_size=8192),
            media_type=upstream.headers.get("Content-Type", "application/octet-stream"),
            headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
        )

    except Exception as e:
        return {"success": False, "message": str(e)}


# -----------------------------
# Audio extraction
#
# Reels/pins don't have a separate audio-only stream, so "download audio"
# means: download the video, pull the audio track out with ffmpeg, serve
# that. Runs in a temp dir that's always cleaned up. Tighter rate limit —
# this is the most CPU/bandwidth-expensive endpoint.
# -----------------------------
@app.get("/extract-audio")
@limiter.limit("10/minute")
def extract_audio(request: Request, url: str, filename: str = "audio.mp3"):
    host = urlparse(url).hostname or ""

    if not any(host == h or host.endswith("." + h) for h in ALLOWED_DOWNLOAD_HOSTS):
        return {"success": False, "message": "This host is not allowed"}

    if shutil.which("ffmpeg") is None:
        return {
            "success": False,
            "message": "ffmpeg is not installed on the server, so audio extraction is unavailable.",
        }

    safe_name = re.sub(r'[\\/*?:"<>|]', "_", filename)
    tmp_dir = tempfile.mkdtemp(prefix="audio_")
    video_path = os.path.join(tmp_dir, "input.mp4")
    audio_path = os.path.join(tmp_dir, "output.mp3")

    try:
        upstream = requests.get(
            url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30
        )
        with open(video_path, "wb") as f:
            f.write(upstream.content)

        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", video_path,
                "-vn", "-acodec", "libmp3lame", "-q:a", "2",
                audio_path,
            ],
            capture_output=True,
            timeout=60,
        )

        if result.returncode != 0 or not os.path.exists(audio_path):
            return {"success": False, "message": "Audio extraction failed"}

        with open(audio_path, "rb") as f:
            audio_bytes = f.read()

        return StreamingResponse(
            iter([audio_bytes]),
            media_type="audio/mpeg",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
        )

    except Exception as e:
        return {"success": False, "message": str(e)}

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

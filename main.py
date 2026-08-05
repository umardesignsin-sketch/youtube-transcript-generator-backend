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

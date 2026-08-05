from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi
import re
import requests

app = FastAPI(title="YouTube Transcript API")

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
# -----------------------------
@app.post("/transcript")
def transcript(data: VideoRequest):

    video_id = extract_video_id(data.url)

    if not video_id:
        return {
            "success": False,
            "message": "Invalid YouTube URL"
        }

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

        return {
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

    except Exception as e:
        return {
            "success": False,
            "message": str(e)
        }
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi
import yt_dlp
import re

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
# Home
# -----------------------------

@app.get("/")
def home():
    return {
        "owner": "UMAR MIRZA",
        "project": "YOUTUBE TRANSCRIPT API",
        "working": True
    }

@app.get("/hello")
def hello():
    return {
        "message": "THIS IS UMAR'S BACKEND"
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

        # -----------------------------
        # Get video information
        # -----------------------------
        ydl_opts = {
            "quiet": True,
            "skip_download": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(data.url, download=False)

        title = info.get("title")
        channel = info.get("uploader")
        thumbnail = info.get("thumbnail")
        duration = info.get("duration")

        # -----------------------------
        # Get transcript
        # -----------------------------
        api = YouTubeTranscriptApi()

        transcript_list = api.list(video_id)

        languages = []

        for t in transcript_list:
            languages.append({
                "language": t.language,
                "language_code": t.language_code,
                "generated": t.is_generated
            })

        # Prefer manually-created transcript
        try:
            transcript_obj = transcript_list.find_manually_created_transcript(
                [t["language_code"] for t in languages]
            )

        except Exception:
            transcript_obj = transcript_list.find_generated_transcript(
                [t["language_code"] for t in languages]
            )

        transcript = transcript_obj.fetch()

        return {
            "success": True,
            "video_id": video_id,
            "title": title,
            "channel": channel,
            "thumbnail": thumbnail,
            "duration": duration,
            "selected_language": transcript_obj.language,
            "selected_language_code": transcript_obj.language_code,
            "generated": transcript_obj.is_generated,
            "available_languages": languages,
            "transcript": transcript.to_raw_data()
        }

    except Exception as e:
        return {
            "success": False,
            "message": str(e)
        }
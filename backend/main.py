
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from uuid import uuid4
from datetime import datetime, timezone
from urllib.parse import urlparse
from pathlib import Path
import os

app = FastAPI(title="Nova Recap AI", version="0.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = Path("/tmp/nova_recap_uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = 500 * 1024 * 1024

ALLOWED_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".webm", ".m4v"
}

jobs = {}


class RecapRequest(BaseModel):
    video_url: str
    recap_type: str = "movie"
    voice: str = "male"
    duration: int = 5


def create_job(source, recap_type, voice, duration):
    job_id = str(uuid4())

    job = {
        "job_id": job_id,
        "status": "received",
        "source": source,
        "recap_type": recap_type,
        "voice": voice,
        "duration": duration,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "message": "Video received successfully"
    }

    jobs[job_id] = job
    return job


@app.get("/")
def home():
    return {
        "status": "Nova Recap AI Backend Ready",
        "version": "0.3.0"
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/jobs")
def create_youtube_job(data: RecapRequest):
    parsed = urlparse(data.video_url)

    host = (parsed.hostname or "").lower()

    allowed_hosts = {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
        "www.youtube.com"
    }

    if parsed.scheme not in ("http", "https") or host not in allowed_hosts:
        raise HTTPException(
            status_code=400,
            detail="Please provide a valid YouTube URL"
        )

    return create_job(
        source={
            "type": "youtube",
            "video_url": data.video_url
        },
        recap_type=data.recap_type,
        voice=data.voice,
        duration=data.duration
    )


@app.post("/api/jobs/upload")
async def upload_video(
    file: UploadFile = File(...),
    recap_type: str = Form("movie"),
    voice: str = Form("male"),
    duration: int = Form(5)
):
    extension = Path(file.filename or "").suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported video format"
        )

    job_id = str(uuid4())
    file_path = UPLOAD_DIR / f"{job_id}{extension}"

    total_size = 0

    try:
        with open(file_path, "wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)

                if not chunk:
                    break

                total_size += len(chunk)

                if total_size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="Video is too large. Maximum size is 500 MB."
                    )

                output.write(chunk)

    except Exception:
        if file_path.exists():
            os.remove(file_path)
        raise

    finally:
        await file.close()

    job = create_job(
        source={
            "type": "upload",
            "filename": file.filename,
            "file_path": str(file_path),
            "size_bytes": total_size
        },
        recap_type=recap_type,
        voice=voice,
        duration=duration
    )

    return job


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(
            status_code=404,
            detail="Job not found"
        )

    return jobs[job_id]

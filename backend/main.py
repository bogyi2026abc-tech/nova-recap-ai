
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from uuid import uuid4
from datetime import datetime, timezone

app = FastAPI(title="Nova Recap AI", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class RecapRequest(BaseModel):
    video_url: str
    recap_type: str = "movie"
    voice: str = "male"
    duration: int = 5

# Temporary job storage
jobs = {}

@app.get("/")
def home():
    return {"status": "Nova Recap AI Backend Ready"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/api/jobs")
def create_job(data: RecapRequest):
    job_id = str(uuid4())

    jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "video_url": data.video_url,
        "recap_type": data.recap_type,
        "voice": data.voice,
        "duration": data.duration,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "message": "Job created successfully"
    }

    return jobs[job_id]

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(
            status_code=404,
            detail="Job not found"
        )

    return jobs[job_id]

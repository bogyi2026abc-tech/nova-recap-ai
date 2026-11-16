
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Nova Recap AI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class RecapRequest(BaseModel):
    video_url: str = ""
    recap_type: str = "movie"
    voice: str = "male"
    duration: int = 5

@app.get("/")
def home():
    return {"status": "Nova Recap AI Backend Ready"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/api/jobs")
def create_job(data: RecapRequest):
    return {
        "status": "received",
        "message": "Backend is connected. AI processing is not configured yet.",
        "voice": data.voice,
        "duration": data.duration
    }

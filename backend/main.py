from __future__ import annotations

import os
import re
import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Optional

import imageio_ffmpeg
import yt_dlp
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
WORK_DIR = Path(os.getenv("NOVA_WORK_DIR", "/tmp/nova-recap-ai"))
WORK_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = WORK_DIR / "uploads"
OUTPUT_DIR = WORK_DIR / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

MAX_UPLOAD_BYTES = 500 * 1024 * 1024
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

app = FastAPI(title="Nova Recap AI", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/files", StaticFiles(directory=str(OUTPUT_DIR)), name="files")


class RecapRequest(BaseModel):
    video_url: Optional[str] = None
    source_url: Optional[str] = None
    recap_type: str = "movie"
    voice: str = "male"
    duration: int = Field(default=5, ge=1, le=30)
    duration_minutes: Optional[int] = Field(default=None, ge=1, le=30)
    speed: float = Field(default=1.0, ge=0.75, le=1.25)
    tone: str = "natural"
    outputs: dict = Field(default_factory=lambda: {"script": True, "subtitles": True, "voice": True, "video": True})


def now():
    return datetime.now(timezone.utc).isoformat()


def update_job(job_id: str, **values):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(values)
            jobs[job_id]["updated_at"] = now()


def get_ffmpeg_duration(path: Path) -> float:
    result = subprocess.run(
        [FFMPEG, "-i", str(path), "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        return 0
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def run_ffmpeg(args: list[str], timeout: int = 1800):
    result = subprocess.run([FFMPEG, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError("FFmpeg error: " + result.stderr[-1800:])
    return result


def download_youtube(url: str, destination: Path) -> Path:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtube-nocookie.com"}
    if parsed.scheme not in {"http", "https"} or host not in allowed:
        raise ValueError("မှန်ကန်တဲ့ YouTube Link ထည့်ပေးပါ။")
    opts = {
        "format": "best[height<=720][ext=mp4]/best[height<=720]/best",
        "outtmpl": str(destination / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_UPLOAD_BYTES,
        "ffmpeg_location": FFMPEG,
        "merge_output_format": "mp4",
        "retries": 2,
        "socket_timeout": 30,
        "extractor_retries": 3,
        "verbose": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
        if not path.exists():
            candidates = list(destination.glob("source.*"))
            if not candidates:
                raise RuntimeError("YouTube Video download မအောင်မြင်ပါ။")
            path = candidates[0]
        return path


def extract_audio_chunks(video_path: Path, folder: Path) -> list[Path]:
    chunks_dir = folder / "audio_chunks"
    chunks_dir.mkdir(exist_ok=True)
    pattern = str(chunks_dir / "part_%03d.mp3")
    run_ffmpeg([
        "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000",
        "-b:a", "64k", "-f", "segment", "-segment_time", "600",
        "-reset_timestamps", "1", pattern
    ])
    chunks = sorted(chunks_dir.glob("part_*.mp3"))
    if not chunks:
        raise RuntimeError("Video ထဲက အသံကို မထုတ်ယူနိုင်ပါ။")
    return chunks


def transcribe_video(client, video_path: Path, folder: Path, job_id: str) -> str:
    chunks = extract_audio_chunks(video_path, folder)
    transcripts = []
    for index, chunk in enumerate(chunks, 1):
        update_job(job_id, status="transcribing", progress=15 + int(25 * index / len(chunks)),
                   message=f"အသံစာသား ပြောင်းနေပါတယ် ({index}/{len(chunks)})")
        with chunk.open("rb") as audio:
            result = client.audio.transcriptions.create(
                model=os.getenv("OPENAI_TRANSCRIPTION_MODEL", "gpt-transcribe"),
                file=audio,
                response_format="text"
            )
        transcripts.append(str(result))
    transcript = "\n\n".join(transcripts).strip()
    if not transcript:
        raise RuntimeError("အသံထဲက စကားပြောစာသား မရရှိပါ။")
    return transcript


def generate_script(client, transcript: str, recap_type: str, duration: int) -> str:
    # Keep the request bounded while retaining the chronological transcript.
    transcript = transcript[:120000]
    target_words = duration * 115
    prompt = f"""
သင်သည် မြန်မာဘာသာဖြင့် ရုပ်ရှင်ဇာတ်လမ်း ပြန်လည်ပြောပြသော အတွေ့အကြုံရှိ Recap ဇာတ်ညွှန်းရေးသူဖြစ်သည်။
အောက်ပါ transcript ကို အခြေခံပြီး {duration} မိနစ်ခန့် ဖတ်ပြနိုင်မည့် မြန်မာ Recap Script တစ်ပုဒ်ရေးပါ။
အမျိုးအစား: {recap_type}
ရည်မှန်းချက်: ခန့်မှန်း {target_words} စကားလုံးဝန်းကျင် (ဇာတ်လမ်းအချက်အလက် မလုံလောက်လျှင် အတင်းမဖြည့်ပါနှင့်)။

စည်းကမ်းများ:
- မြန်မာလိုသာ ရေးပါ။ မလိုအပ်သော English စကားလုံး မထည့်ပါနှင့်။
- စစချင်း ၁-၂ ကြောင်းမှာ စိတ်ဝင်စားစရာ Hook အားကောင်းကောင်း ပါရမည်။
- ဇာတ်လမ်းဖြစ်စဉ်ကို အစဉ်လိုက်၊ သဘာဝကျပြီး လူတစ်ယောက် ဖတ်ပြနေသလို ချောမွေ့စွာ ရေးပါ။
- စာကြောင်းတိုတို၊ နားထောင်ရလွယ်သော စကားပြောဟန်ကို သုံးပါ။
- Transcript မှာ မပါသော ဇာတ်ကောင်၊ ဖြစ်ရပ်၊ နောက်ဆုံးရလဒ်တို့ကို မတီထွင်ပါနှင့်။ မရှင်းလင်းသောနေရာကို မခန့်မှန်းပါနှင့်။
- အဆုံးမှာ နောက်ဆက်တွဲကို သိချင်စေမည့် သဘာဝကျသော မေးခွန်းတစ်ခု ထည့်ပါ။
- ခေါင်းစဉ်၊ အချိန်မှတ်တမ်း၊ မှတ်ချက်များ မထည့်ဘဲ ဖတ်ပြမည့် Script သာ ထုတ်ပါ။

TRANSCRIPT:
{transcript}
"""
    response = client.responses.create(
        model=os.getenv("OPENAI_TEXT_MODEL", "gpt-4.1-mini"),
        input=prompt
    )
    text = (getattr(response, "output_text", "") or "").strip()
    if not text:
        raise RuntimeError("AI က Recap Script မထုတ်ပေးနိုင်ပါ။")
    return text


def split_text(text: str, limit: int = 2800) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    chunks, current = [], ""
    for paragraph in paragraphs:
        while len(paragraph) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        candidate = (current + "\n\n" + paragraph).strip()
        if len(candidate) > limit and current:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def create_voice(client, script: str, folder: Path, voice_type: str, speed: float, tone: str) -> Path:
    voice_name = "onyx" if voice_type == "male" else "nova"
    instructions = {
        "natural": "Speak Burmese clearly and naturally, like a friendly professional movie recap narrator. Use expressive but controlled delivery.",
        "dramatic": "Speak Burmese with cinematic suspense and dramatic emphasis, while keeping every word clear.",
        "calm": "Speak Burmese in a calm, warm, steady narration style."
    }.get(tone, "Speak Burmese clearly and naturally with expressive narration.")
    parts = []
    for index, chunk in enumerate(split_text(script), 1):
        response = client.audio.speech.create(
            model=os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
            voice=voice_name,
            input=chunk,
            instructions=instructions,
            speed=speed,
            response_format="mp3"
        )
        part = folder / f"voice_{index:03d}.mp3"
        response.write_to_file(str(part))
        parts.append(part)
    if not parts:
        raise RuntimeError("အသံထုတ်ရန် Script မရှိပါ။")
    if len(parts) == 1:
        output = folder / "narration.mp3"
        shutil.copyfile(parts[0], output)
        return output
    concat_file = folder / "voice_concat.txt"
    concat_file.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    output = folder / "narration.mp3"
    run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(output)])
    return output


def make_srt(script: str, audio_duration: float, output: Path):
    sentences = [s.strip() for s in re.split(r"(?<=[။!?])\s*|\n+", script) if s.strip()]
    if not sentences:
        sentences = [script]
    weights = [max(1, len(s)) for s in sentences]
    total = sum(weights)
    cursor = 0.0
    lines = []
    for i, (sentence, weight) in enumerate(zip(sentences, weights), 1):
        start = cursor
        end = audio_duration if i == len(sentences) else cursor + audio_duration * weight / total
        def stamp(seconds):
            ms = int(max(0, seconds) * 1000)
            h, rem = divmod(ms, 3600000)
            m, rem = divmod(rem, 60000)
            sec, milli = divmod(rem, 1000)
            return f"{h:02}:{m:02}:{sec:02},{milli:03}"
        lines.extend([str(i), f"{stamp(start)} --> {stamp(end)}", sentence, ""])
        cursor = end
    output.write_text("\n".join(lines), encoding="utf-8")


def render_mp4(video_path: Path, narration: Path, output: Path, audio_duration: float):
    run_ffmpeg([
        "-y", "-stream_loop", "-1", "-i", str(video_path), "-i", str(narration),
        "-t", f"{max(1, audio_duration):.2f}",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output)
    ], timeout=3600)


def process_job(job_id: str, source_type: str, source_value: str, settings: dict):
    folder = WORK_DIR / job_id
    folder.mkdir(parents=True, exist_ok=True)
    try:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("Render Environment မှာ OPENAI_API_KEY မထည့်ရသေးပါ။")
        if OpenAI is None:
            raise RuntimeError("OpenAI package မတွေ့ပါ။ requirements.txt ကို စစ်ပါ။")
        client = OpenAI()
        update_job(job_id, status="downloading", progress=5, message="Video ကို ရယူနေပါတယ်")
        video_path = Path(source_value) if source_type == "upload" else download_youtube(source_value, folder)
        if not video_path.exists():
            raise RuntimeError("Video ဖိုင်ကို ရှာမတွေ့ပါ။")

        transcript = transcribe_video(client, video_path, folder, job_id)
        (folder / "transcript.txt").write_text(transcript, encoding="utf-8")

        update_job(job_id, status="writing", progress=45, message="မြန်မာ Recap Script ရေးနေပါတယ်")
        duration = int(settings.get("duration_minutes") or settings.get("duration") or 5)
        script = generate_script(client, transcript, settings.get("recap_type", "movie"), duration)
        script_path = OUTPUT_DIR / f"{job_id}_recap.txt"
        script_path.write_text(script, encoding="utf-8")

        outputs = settings.get("outputs") or {}
        result_files = {"script": f"/files/{script_path.name}"}
        if outputs.get("voice", True) or outputs.get("video", True):
            update_job(job_id, status="voicing", progress=65, message="မြန်မာ AI အသံ ဖန်တီးနေပါတယ်")
            narration = create_voice(client, script, folder, settings.get("voice", "male"),
                                     float(settings.get("speed", 1.0)), settings.get("tone", "natural"))
            voice_out = OUTPUT_DIR / f"{job_id}_voice.mp3"
            shutil.copyfile(narration, voice_out)
            result_files["voice"] = f"/files/{voice_out.name}"
            voice_duration = get_ffmpeg_duration(narration)
            if outputs.get("subtitles", True):
                srt_path = OUTPUT_DIR / f"{job_id}_subtitles.srt"
                make_srt(script, voice_duration, srt_path)
                result_files["subtitles"] = f"/files/{srt_path.name}"
            if outputs.get("video", True):
                update_job(job_id, status="rendering", progress=82, message="MP4 Video ပြုလုပ်နေပါတယ်")
                mp4_path = OUTPUT_DIR / f"{job_id}_recap.mp4"
                render_mp4(video_path, narration, mp4_path, voice_duration)
                result_files["video"] = f"/files/{mp4_path.name}"

        update_job(job_id, status="completed", progress=100, message="Recap ပြီးပါပြီ", files=result_files,
                   script=script, transcript_preview=transcript[:500])
    except Exception as exc:
        update_job(job_id, status="failed", progress=100, message="လုပ်ဆောင်မှု မအောင်မြင်ပါ", error=str(exc))
    finally:
        # Keep generated outputs, but remove temporary source/audio work to reduce disk use.
        try:
            shutil.rmtree(folder, ignore_errors=True)
        except Exception:
            pass


def start_job(source_type: str, source_value: str, settings: dict, background: BackgroundTasks):
    job_id = str(uuid4())
    jobs[job_id] = {
        "job_id": job_id, "status": "queued", "progress": 0, "message": "စတင်ရန် စောင့်ဆိုင်းနေပါတယ်",
        "created_at": now(), "updated_at": now(), "files": {}
    }
    background.add_task(process_job, job_id, source_type, source_value, settings)
    return jobs[job_id]


@app.get("/")
def home():
    index = PROJECT_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"status": "Nova Recap AI Backend Ready", "version": "1.0.0", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/config")
def config():
    return {"openai_configured": bool(os.getenv("OPENAI_API_KEY")), "max_upload_mb": 500,
            "features": ["youtube", "upload", "burmese-recap", "burmese-tts", "subtitles", "mp4"]}


@app.post("/api/jobs")
def create_youtube_job(data: RecapRequest, background: BackgroundTasks):
    url = data.video_url or data.source_url
    if not url:
        raise HTTPException(status_code=400, detail="YouTube URL ထည့်ပေးပါ။")
    return start_job("youtube", url, data.model_dump(), background)


@app.post("/api/jobs/upload")
async def upload_video(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    recap_type: str = Form("movie"),
    voice: str = Form("male"),
    duration: int = Form(5),
    speed: float = Form(1.0),
    tone: str = Form("natural"),
    subtitles: bool = Form(True),
    make_video: bool = Form(True)
):
    extension = Path(file.filename or "").suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="MP4, MOV, MKV, WEBM ဖိုင်များသာ လက်ခံပါတယ်။")
    job_id = str(uuid4())
    file_path = UPLOAD_DIR / f"{job_id}{extension}"
    size = 0
    try:
        with file_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Video အများဆုံး 500 MB အထိသာ တင်နိုင်ပါတယ်။")
                out.write(chunk)
    except Exception:
        file_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    settings = {
        "recap_type": recap_type, "voice": voice, "duration": duration, "duration_minutes": duration,
        "speed": speed, "tone": tone,
        "outputs": {"script": True, "subtitles": subtitles, "voice": True, "video": make_video}
    }
    job = {
        "job_id": job_id, "status": "queued", "progress": 0, "message": "Video ကို လက်ခံရရှိပါပြီ",
        "created_at": now(), "updated_at": now(), "files": {}
    }
    jobs[job_id] = job
    background.add_task(process_job, job_id, "upload", str(file_path), settings)
    return job



@app.post("/api/blur/upload")
async def blur_video_upload(
    file: UploadFile = File(...),
    region_json: str = Form(...),
    start_time: float = Form(0),
    end_time: float = Form(...),
    strength: int = Form(12),
    mode: str = Form("blur")
):
    """Apply a selected time-bounded blur or mosaic rectangle to an uploaded video."""
    extension = Path(file.filename or "").suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="MP4, MOV, MKV, WEBM ဖိုင်များသာ လက်ခံပါတယ်။")
    if start_time < 0 or end_time <= start_time:
        raise HTTPException(status_code=400, detail="စ/ဆုံး အချိန် မမှန်ပါ။")
    if not 2 <= strength <= 30 or mode not in {"blur", "mosaic"}:
        raise HTTPException(status_code=400, detail="Blur setting မမှန်ပါ။")
    try:
        region = json.loads(region_json)
        x, y, w, h = [float(region[k]) for k in ("x", "y", "w", "h")]
    except Exception:
        raise HTTPException(status_code=400, detail="ဝါးမယ့်နေရာကို မှန်ကန်စွာ ရွေးပါ။")
    if any(v < 0 or v > 1 for v in (x, y, w, h)) or w <= 0 or h <= 0 or x+w > 1.001 or y+h > 1.001:
        raise HTTPException(status_code=400, detail="ရွေးထားတဲ့ နေရာအကျယ်အဝန်း မမှန်ပါ။")
    job_id = str(uuid4())
    source = UPLOAD_DIR / f"{job_id}{extension}"
    output = OUTPUT_DIR / f"{job_id}_blurred.mp4"
    size = 0
    try:
        with source.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk: break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Video အများဆုံး 500 MB အထိသာ တင်နိုင်ပါတယ်။")
                out.write(chunk)
    finally:
        await file.close()
    try:
        probe = subprocess.run([FFMPEG, "-i", str(source)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", probe.stderr, re.S)
        if not match: raise RuntimeError("Video အရွယ်အစားကို မဖတ်နိုင်ပါ။")
        vw, vh = int(match.group(1)), int(match.group(2))
        rx, ry = max(0,min(vw-2,int(x*vw))), max(0,min(vh-2,int(y*vh)))
        rw, rh = max(2,min(vw-rx,int(w*vw))), max(2,min(vh-ry,int(h*vh)))
        rx, ry, rw, rh = rx//2*2, ry//2*2, max(2,rw//2*2), max(2,rh//2*2)
        if mode == "blur":
            effect = f"boxblur={max(2,strength)}:2"
        else:
            block=max(4,strength*2)
            effect=f"scale=iw/{block}:ih/{block}:flags=neighbor,scale={rw}:{rh}:flags=neighbor"
        enable=f"between(t,{start_time:.3f},{end_time:.3f})"
        graph=f"[0:v]split[base][patch];[patch]crop={rw}:{rh}:{rx}:{ry},{effect}[fx];[base][fx]overlay={rx}:{ry}:enable='{enable}'[vout]"
        run_ffmpeg(["-y","-i",str(source),"-filter_complex",graph,"-map","[vout]","-map","0:a?","-c:v","libx264","-preset","veryfast","-crf","23","-c:a","aac","-b:a","128k","-movflags","+faststart",str(output)],timeout=3600)
        return {"status":"completed","file_url":f"/files/{output.name}"}
    except HTTPException:
        raise
    except Exception as exc:
        output.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        source.unlink(missing_ok=True)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job မတွေ့ပါ။ Server ပြန်စတင်ထားလျှင် အဟောင်း Job များ ပျောက်နိုင်ပါတယ်။")
    return jobs[job_id]

# Nova Recap AI

A FastAPI-based Burmese recap prototype with a mobile-friendly web interface.

## Features

- Submit a YouTube URL or upload an MP4/MOV/MKV/WEBM/M4V file (maximum 500 MB).
- Extract audio with FFmpeg and transcribe it in chunks.
- Generate a Burmese recap script using the OpenAI Responses API.
- Generate Burmese narration using the OpenAI Speech API (male/female voice presets, speed and delivery style).
- Create an SRT subtitle file.
- Render an MP4 using the source video and generated narration.

## Render deployment

The backend is in the `backend` directory. Configure the Render Web Service:

- Root Directory: `backend`
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`

Set these environment variables in Render (never commit API keys):

- `OPENAI_API_KEY` — required.
- `OPENAI_TEXT_MODEL` — optional; defaults to `gpt-4.1-mini`.
- `OPENAI_TRANSCRIPTION_MODEL` — optional; defaults to `gpt-transcribe`.
- `OPENAI_TTS_MODEL` — optional; defaults to `gpt-4o-mini-tts` (check OpenAI model lifecycle before long-term production use).
- `NOVA_WORK_DIR` — optional; defaults to `/tmp/nova-recap-ai`.

After deployment, check `/health` and `/api/config`. The latter reports whether the API key is present without exposing it.

## API

- `GET /` — web app
- `GET /health` — health check
- `GET /api/config` — configuration status
- `POST /api/jobs` — JSON YouTube job
- `POST /api/jobs/upload` — multipart video upload
- `GET /api/jobs/{job_id}` — job status and generated file links

## Important production notes

- This version uses in-process background tasks and in-memory job state. A restart or multiple Render instances can lose job status. Use a persistent queue/database for production.
- Generated files are stored under `/tmp` by default and are ephemeral on Render. Configure persistent storage or external object storage before relying on long-term downloads.
- Large videos and long recaps consume API credits and server disk/CPU. The 500 MB upload limit is a hard upper bound, not a recommended input size.
- YouTube downloads depend on yt-dlp compatibility and the video's access restrictions. Use only content you own or are authorized to process.
- The SRT timing is estimated from sentence length, not word-level speech alignment.
- Test Burmese transcription and voice quality with representative material before production use.

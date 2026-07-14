from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import uuid
import os
import asyncio
from typing import Optional

app = FastAPI(title="VideoVault API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Models ───────────────────────────────────────────────────────────────────

class VideoInfoRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    quality: Optional[str] = "best"   # best / 1080 / 720 / 480 / 360

# ─── Helpers ──────────────────────────────────────────────────────────────────

def detect_platform(url: str) -> str:
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    elif "tiktok.com" in url:
        return "tiktok"
    elif "instagram.com" in url:
        return "instagram"
    elif "twitter.com" in url or "x.com" in url:
        return "twitter"
    elif "facebook.com" in url or "fb.watch" in url:
        return "facebook"
    elif "vimeo.com" in url:
        return "vimeo"
    return "other"

def quality_to_format(quality: str) -> str:
    formats = {
        "best":  "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "1080":  "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "720":   "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
        "480":   "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best",
        "360":   "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best",
    }
    return formats.get(quality, formats["best"])

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "service": "VideoVault API"}

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/info")
def get_video_info(req: VideoInfoRequest):
    """
    Возвращает информацию о видео:
    - title, duration, thumbnail
    - доступные качества
    - платформа
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 30,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)

            # Собираем доступные качества
            qualities = set()
            for f in info.get("formats", []):
                h = f.get("height")
                ext = f.get("ext", "")
                if h and ext in ("mp4", "webm", "m4v"):
                    if h >= 360:
                        qualities.add(h)

            sorted_qualities = sorted(qualities, reverse=True)
            quality_labels = [str(q) for q in sorted_qualities]
            if not quality_labels:
                quality_labels = ["best"]

            return {
                "success": True,
                "title": info.get("title", "Unknown"),
                "duration": info.get("duration", 0),          # секунды
                "thumbnail": info.get("thumbnail"),
                "platform": detect_platform(req.url),
                "uploader": info.get("uploader", ""),
                "view_count": info.get("view_count", 0),
                "qualities": quality_labels,
            }

    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=f"Could not fetch video: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/download-url")
def get_download_url(req: DownloadRequest):
    """
    Возвращает прямую ссылку на скачивание видео.
    Приложение скачивает видео напрямую по этой ссылке.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": quality_to_format(req.quality),
        "socket_timeout": 30,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)

            # Ищем лучший mp4 формат
            formats = info.get("formats", [])
            best_url = None
            best_height = 0

            target_height = int(req.quality) if req.quality.isdigit() else 9999

            for f in reversed(formats):
                h = f.get("height") or 0
                ext = f.get("ext", "")
                url = f.get("url", "")
                vcodec = f.get("vcodec", "none")
                acodec = f.get("acodec", "none")

                # Ищем формат с видео и аудио вместе (progressive)
                if (ext == "mp4" and vcodec != "none" and acodec != "none"
                        and h <= target_height and h > best_height and url):
                    best_url = url
                    best_height = h

            # Если нет progressive, берём любой подходящий
            if not best_url:
                for f in reversed(formats):
                    url = f.get("url", "")
                    h = f.get("height") or 0
                    if url and h <= target_height:
                        best_url = url
                        best_height = h
                        break

            if not best_url:
                # Последний вариант — url самого info
                best_url = info.get("url")

            if not best_url:
                raise HTTPException(status_code=404, detail="No downloadable format found")

            return {
                "success": True,
                "download_url": best_url,
                "title": info.get("title", "video"),
                "ext": "mp4",
                "filesize": info.get("filesize") or info.get("filesize_approx", 0),
                "height": best_height,
                "platform": detect_platform(req.url),
            }

    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=f"Download error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

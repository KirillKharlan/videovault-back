from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
from typing import Optional

app = FastAPI(title="VideoVault API", version="2.0.0")

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
    """
    Converts quality label to yt-dlp format string.
    yt-dlp 2026.07.04 prefers mp4 progressive where possible,
    falls back to bestvideo+bestaudio merge.
    """
    if quality == "best" or not quality.isdigit():
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"
    h = quality
    return (
        f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={h}]+bestaudio"
        f"/best[height<={h}][ext=mp4]"
        f"/best[height<={h}]"
        f"/best"
    )

def base_ydl_opts() -> dict:
    """
    Common yt-dlp options tuned for 2026.07.04.
    - player_client: ['ios', 'web'] gives best compatibility
      iOS client bypasses many bot-detection issues on YouTube.
    - No cookies needed for public videos.
    """
    return {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "nocheckcertificate": False,
        "extractor_args": {
            # Use iOS client first — most stable for public YouTube videos
            # in the 2026.x era (avoids po_token requirement on server-side)
            "youtube": {
                "player_client": ["ios", "web"],
            }
        },
        "http_headers": {
            "User-Agent": (
                "com.google.ios.youtube/19.45.4 "
                "(iPhone16,2; U; CPU iOS 18_1_0 like Mac OS X)"
            ),
        },
    }

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "service": "VideoVault API", "yt_dlp": yt_dlp.version.__version__}

@app.get("/health")
def health():
    return {"status": "ok", "yt_dlp_version": yt_dlp.version.__version__}


@app.post("/info")
def get_video_info(req: VideoInfoRequest):
    """
    Returns video metadata:
    title, duration, thumbnail, platform, available qualities.
    """
    opts = {
        **base_ydl_opts(),
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(req.url, download=False)

            # Collect available MP4 qualities
            qualities: set[int] = set()
            for f in info.get("formats", []):
                h = f.get("height")
                ext = f.get("ext", "")
                vcodec = f.get("vcodec", "none")
                # Only list formats that actually have video
                if h and h >= 240 and vcodec != "none":
                    qualities.add(h)

            sorted_q = sorted(qualities, reverse=True)
            quality_labels = [str(q) for q in sorted_q] or ["best"]

            return {
                "success": True,
                "title": info.get("title", "Unknown"),
                "duration": info.get("duration", 0),
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
    Returns a direct download URL for the video.
    The mobile app downloads the file directly from this URL.

    Strategy for yt-dlp 2026.07.04:
    1. Try to find a single-file mp4 (video+audio, "progressive")
    2. If not found, return the best video-only stream
       (the app will need to handle audio separately — or we pick
        the best combined format yt-dlp resolves)
    """
    opts = {
        **base_ydl_opts(),
        "skip_download": True,
        "format": quality_to_format(req.quality or "best"),
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(req.url, download=False)

            formats = info.get("formats", [])
            target_height = int(req.quality) if (req.quality or "").isdigit() else 9999

            # 1st pass: look for combined mp4 (vcodec + acodec, single file)
            best: dict | None = None
            best_height = 0
            for f in formats:
                h = f.get("height") or 0
                ext = f.get("ext", "")
                vcodec = f.get("vcodec", "none")
                acodec = f.get("acodec", "none")
                url = f.get("url", "")
                if (ext == "mp4" and vcodec != "none" and acodec != "none"
                        and h <= target_height and h > best_height and url):
                    best = f
                    best_height = h

            # 2nd pass: any format with video
            if not best:
                for f in reversed(formats):
                    h = f.get("height") or 0
                    url = f.get("url", "")
                    vcodec = f.get("vcodec", "none")
                    if url and vcodec != "none" and h <= target_height:
                        best = f
                        best_height = h
                        break

            # Last resort: top-level url
            if not best:
                direct = info.get("url")
                if not direct:
                    raise HTTPException(status_code=404, detail="No downloadable format found")
                return {
                    "success": True,
                    "download_url": direct,
                    "title": info.get("title", "video"),
                    "ext": info.get("ext", "mp4"),
                    "filesize": info.get("filesize") or info.get("filesize_approx") or 0,
                    "height": 0,
                    "platform": detect_platform(req.url),
                    # Pass headers so the app can authenticate the download request
                    "headers": dict(info.get("http_headers", {})),
                }

            return {
                "success": True,
                "download_url": best["url"],
                "title": info.get("title", "video"),
                "ext": best.get("ext", "mp4"),
                "filesize": best.get("filesize") or best.get("filesize_approx") or 0,
                "height": best_height,
                "platform": detect_platform(req.url),
                # Important: some platforms (YouTube iOS) require
                # the original request headers to download the file
                "headers": dict(best.get("http_headers", info.get("http_headers", {}))),
            }

    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=f"Download error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

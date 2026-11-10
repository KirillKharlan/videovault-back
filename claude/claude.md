# CLAUDE.md — videovault-backend

Context file for Claude. Read this first so you don't have to re-scan the whole repo for every fix. Update it when the structure changes.

## What this is

Flask backend that wraps **yt-dlp** for the VideoVault Flutter app. The app sends a video URL, the backend fetches info (title, duration, available qualities), downloads the video/audio in a background task, reports progress, and serves the finished file. Deployed on **Render** (Docker runtime, free plan).

## Repo layout

```
videovault-backend/
├── app.py                  # Entire backend (Flask, ~1000 lines)
├── ytdlp_worker.py         # yt-dlp as a library, run as a SUBPROCESS of app.py
├── start.sh                # Entry point: upgrades yt-dlp + bgutil plugin, then gunicorn
├── Dockerfile              # python:3.11-slim + ffmpeg + Deno
├── render.yaml             # Two Render services: backend + PO-token provider
├── requirements.txt        # Flask / flask-cors / gunicorn (yt-dlp is NOT here)
├── README_DEPLOY.md        # Deploy notes (Render, env vars, cookies, /health)
├── PROJECT_MAP_BACKEND.md  # Russian map of app.py / worker / API (kept in sync)
└── *cookies*.txt           # LOCAL secrets for generating YT_COOKIES_B64
```

## Architecture

- **app.py** (gunicorn, 1 worker, 300s timeout) never imports yt-dlp. It launches `ytdlp_worker.py` as a separate process per operation.
- **Why a subprocess:** if a video OOM-kills the process, only the worker dies. Parent sees `returncode < 0` and the quality-downgrade logic kicks in (see `QUALITY_LADDER`, `_next_lower_quality`).
- **Worker protocol:** config arrives as one JSON line on stdin; results go to stdout as one JSON event per line (`info`, progress, etc.); errors go to stderr as plain exception text, which `classify_error()` in app.py pattern-matches.
- **Worker modes:** `info` (metadata only), `download` (extract + download to a file in one pass; reusing info from `/api/info` was abandoned because YouTube signed URLs expire too fast) and `stream` (single mp4 written to the worker's stdout; **nothing else may be printed to stdout in this mode**).
- **`POST /api/fetch` (the app's main path):** one request, video comes back immediately. If the cached `/api/info` shows a single http(s) mp4 (audio+video in one file) that is not worse than the best video-only track, the worker streams it straight to the client (`_plan_stream`, `_open_stream`, `_stream_body`); otherwise it returns `{"mode":"task","task_id"}` and the old flow (`download_task`, `/api/progress`, `/api/file`) runs unchanged. Any failure before the first byte falls back to the task path. YouTube 720p+ (video+audio merge) still uses the task path. Merging on the fly (two yt-dlp + ffmpeg pipes) was NOT built: untested, RAM risk on 512 MB.
- **Tasks are stored on disk** (`tasks.json` next to app.py), not in memory, so they survive process restarts. `reconcile_stale_tasks()` cleans up tasks left over from a restart. A background `cleanup_loop` removes old files/tasks.
- **PO Token:** `bgutil-ytdlp-pot-provider` plugin (installed in start.sh) talks to a separate Render service (`videovault-pot-provider`) via `POT_PROVIDER_URL`. Without it, formats needing a PO token silently disappear, so the worker passes the option that keeps them in the list (they may 403 at download time and fall back to the next client/proxy).
- **Deno** is required in the image for yt-dlp YouTube signature extraction (`yt-dlp-ejs`).

## app.py map (top to bottom)

1. Imports, Flask init, CORS, request logging (`DEBUG_VERBOSE` controls log tail length)
2. Paths: `TMP_DIR`, `WORKER_SCRIPT`, `TASKS_FILE`, `COOKIES_FILE`; `INFO_CACHE_TTL = 600`; `STALL_TIMEOUT = 90`
3. Task storage: `load_tasks`, `save_tasks`, `get_task`, `set_task`, `update_task`
4. `setup_cookies()` — writes `cookies.txt` from env `YT_COOKIES_B64`
5. `reconcile_stale_tasks()`, `cleanup_loop()`
6. `ERROR_PATTERNS` / `classify_error()` — raw stderr to user-facing error type and message
7. `QUALITY_LADDER` (2160 → 240) and `_next_lower_quality()`
8. Proxy pool: `_load_proxy_pool`, `select_proxy`, `mark_proxy_account_dead` (dead accounts cooled down for 6h; env `YT_PROXIES` or `YT_PROXY`)
9. Format selection: `_best_video_format_id`, `_best_audio_format_id`, `_resolve_format_selector`
10. Info cache + negative cache: `get_cached_raw_info`, `peek_cached_info`, `peek_negative_info`, `get_video_info`. `DEFINITIVE_ERROR_TYPES` (private, age_restricted, geo_blocked, not_found, copyright) are not retried.
11. Worker runners: `_run_worker_once`, `_run_worker_streaming`
12. Client fallback: `_try_one_client_for_info`, `_client_order`, `_fetch_video_info_uncached` (tries player clients in parallel batches of `INFO_PARALLEL_BATCH = 3`); remembers the best-working client globally and per URL
13. `download_task(task_id, url, quality)` — background download with client/proxy fallback and quality downgrade
14. Routes

## API

| Route | Purpose |
|---|---|
| `GET /health` | Status, yt-dlp version, cookies present, task count |
| `POST /api/info` | Video metadata and available qualities |
| `POST /api/download` | Creates `task_id`, starts `download_task` in a thread, returns immediately |
| `GET /api/progress/<task_id>` | Task status and progress |
| `GET /api/file/<task_id>` | Serves the finished file |

## Environment variables

- `YT_COOKIES_B64` — base64 of the YouTube cookies file. Used only by the last two `CLIENT_ATTEMPTS` (`web`, `tv` with cookies); anonymous clients go first because stale cookies from a datacenter IP hurt more than help
- `POT_PROVIDER_URL` — URL of the bgutil PO-token service (scheme is added if missing)
- `YT_PROXIES` / `YT_PROXY` — proxy pool / single proxy
- `DEBUG_VERBOSE` — `1`/`true`/`yes` for longer stderr tails in logs
- `PORT` — set by Render (default 8000)

## Known quirks

- **Render free plan sleeps** after ~15 min idle; the first request takes 15–30s. The Flutter client needs 90s+ timeouts on the first call.
- **yt-dlp is upgraded at every start** in `start.sh` (not pinned). A breaking yt-dlp release can break prod on the next restart.
- **YouTube Shorts** can return empty formats/duration on some clients; "poor data" results are treated as a failed client and the next one is tried.
- **`gunicorn --workers 1 --threads 8`**: threads are required, otherwise a streamed response would block every other request. Workers stay at 1 (shared disk task file, in-process caches); don't raise workers without checking the caches and `tasks.json` access.

## Quality list (`/api/info`)
Different YouTube clients return different format sets (one may give only `best`/240p, another up to 4K). `_fetch_video_info_uncached` therefore picks the **best** result (`_info_rank`), not the first successful one, and only records a client as globally best if it gave >= 480p (previously a weak client won first and kept being tried first). Response also has `quality_sizes` (estimated bytes per height) and `/api/info` accepts `refresh: true` to bypass the cache.

## Docs status

`README_DEPLOY.md` and `PROJECT_MAP_BACKEND.md` were rewritten in Nov 2026 to match the code (Docker + `start.sh` + gunicorn
`app:app`, `CLIENT_ATTEMPTS`, worker subprocess, proxies, quality ladder). When code and docs disagree, trust Dockerfile / start.sh /
render.yaml / the code, then fix the docs.

- `requirements.txt` still lists Flask/CORS/gunicorn from GitHub `main`/`master` zips and is **not** used by the Docker build;
  the Dockerfile's `pip install` (flask 3.1.0, flask-cors 5.0.0, gunicorn 23.0.0) is what actually runs.

## Security

- `cookies.txt`, `www.youtube.com_cookies.txt`, and `encoded_cookies.txt` are account credentials. `.gitignore` lists only the first two; **`encoded_cookies.txt` is not ignored**. Add it, and never commit or paste any of these.
- CORS is open to all origins.

## Debugging checklist

1. Look at the error type shown in the app (text comes from `ERROR_PATTERNS`).
2. "unsupported" / "format unavailable" on one video: check the client fallback in `_fetch_video_info_uncached` / `download_task` and the PO-token service.
3. "cookies" / "bot": check `setup_cookies()` and `YT_COOKIES_B64` on Render; cookies may have expired.
4. "Task not found": server likely restarted mid-task; check `reconcile_stale_tasks` and Render restart logs.
5. Proxy errors (e.g. 402): account is marked dead for 6h; check `YT_PROXIES`.
6. Ask for the Render logs: `[DEBUG]` lines contain the exact yt-dlp stderr.

## Working rules for Claude

- Make minimal, targeted edits to `app.py` / `ytdlp_worker.py`; keep the subprocess isolation.
- Don't add yt-dlp to `requirements.txt` or import it inside Flask.
- Don't print or log cookie contents or proxy credentials.
- After structural changes, update this file.

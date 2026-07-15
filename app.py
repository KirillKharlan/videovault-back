"""
VideoVault Backend
Fixes:
  1. YouTube bot detection → cookies передаются через env переменную
  2. "Задача не найдена" → задачи хранятся в файле на диске (переживают sleep)
"""

import os, json, threading, subprocess, time, uuid, tempfile, re, base64
from pathlib import Path
from flask import Flask, request, jsonify, send_file, abort
from flask_cors import CORS

app = Flask(__name__)
@app.before_request
def log_request_info():
    print(f"[REQUEST] {request.method} {request.url}")
    print(f"[REQUEST BODY] {request.get_data()}")
CORS(app, origins="*")


BASE_DIR = Path(__file__).resolve().parent
TMP_DIR = Path(tempfile.gettempdir()) / "videovault"
TMP_DIR.mkdir(exist_ok=True)

# Файл для хранения задач — переживает sleep сервера
TASKS_FILE = BASE_DIR / "tasks.json"
COOKIES_FILE = BASE_DIR / "cookies.txt"
_tasks_lock = threading.Lock()


# ── Хранение задач на диске ───────────────────────────────────────────────────

_tasks_cache = {}

def load_tasks() -> dict:
    global _tasks_cache
    if _tasks_cache:
        return _tasks_cache
    try:
        if TASKS_FILE.exists():
            _tasks_cache = json.loads(TASKS_FILE.read_text())
            return _tasks_cache
    except Exception:
        pass
    _tasks_cache = {}
    return _tasks_cache

def save_tasks(tasks: dict):
    try:
        TASKS_FILE.write_text(json.dumps(tasks))
    except Exception as e:
        print(f"[ERROR] Save tasks failed: {e}")

def get_task(task_id: str) -> dict | None:
    with _tasks_lock:
        return load_tasks().get(task_id)

def set_task(task_id: str, data: dict):
    with _tasks_lock:
        tasks = load_tasks()
        tasks[task_id] = data
        _tasks_cache = tasks
        save_tasks(tasks)

def update_task(task_id: str, **kw):
    with _tasks_lock:
        tasks = load_tasks()
        if task_id in tasks:
            tasks[task_id].update(kw)
            _tasks_cache = tasks
            if kw.get("status") in ["done", "error", "queued"]:
                save_tasks(tasks)


# ── Cookies из env переменной ─────────────────────────────────────────────────

def setup_cookies() -> str | None:
    """
    Записывает cookies.txt из env и возвращает путь к нему.
    Если env не задан — возвращает None.
    """
    
    print(f"[DEBUG] Cookies file exists: {COOKIES_FILE.exists()}, size: {COOKIES_FILE.stat().st_size if COOKIES_FILE.exists() else 0} bytes")
    
    b64 = os.environ.get("YT_COOKIES_B64", "").strip()
    if not b64:
        return None
    try:
        decoded = base64.b64decode(b64).decode("utf-8")
        COOKIES_FILE.write_text(decoded)
        return str(COOKIES_FILE)
    except Exception as e:
        print(f"[cookies] Failed to decode: {e}")
        return None

setup_cookies()  # Записываем cookies при старте


# ── Очистка старых файлов ─────────────────────────────────────────────────────

def cleanup_loop():
    while True:
        time.sleep(3600)
        now = time.time()
        with _tasks_lock:
            tasks = load_tasks()
            to_del = []
            for tid, t in tasks.items():
                if now - t.get("created_at", now) > 7200:
                    fp = t.get("file_path")
                    if fp:
                        Path(fp).unlink(missing_ok=True)
                    to_del.append(tid)
            for tid in to_del:
                if tid in tasks:
                    del tasks[tid]
            save_tasks(tasks)

threading.Thread(target=cleanup_loop, daemon=True).start()


# ── Классификатор ошибок ──────────────────────────────────────────────────────

ERROR_PATTERNS = [
    (["sign in to confirm", "confirm you're not a bot", "bot detection"],
     "bot", "🤖 YouTube требует авторизацию — нужно настроить cookies (см. README)"),
    (["private video", "video is private"],
     "private", "🔒 Видео приватное"),
    (["age-restricted", "sign in to confirm your age"],
     "age_restricted", "🔞 Видео с ограничением по возрасту"),
    (["not available in your country", "blocked in your country"],
     "geo_blocked", "🌍 Видео недоступно в регионе сервера"),
    (["video unavailable", "has been removed", "does not exist"],
     "not_found", "🔍 Видео не найдено или удалено"),
    (["copyright", "removed by"],
     "copyright", "©️ Видео удалено за нарушение авторских прав"),
    (["unsupported url", "no video formats", "unable to extract"],
     "unsupported", "❌ Эта ссылка не поддерживается"),
    (["urlopen error", "connection", "timed out"],
     "network", "📡 Ошибка сети"),
]

def classify_error(text: str) -> tuple[str, str]:
    low = text.lower()
    for patterns, err_type, message in ERROR_PATTERNS:
        if any(p in low for p in patterns):
            return err_type, message
    return "unknown", f"⚠️ Ошибка: {text[:300]}"


# ── yt-dlp аргументы ──────────────────────────────────────────────────────────

def ytdlp_base_args() -> list[str]:
    args = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        # Оптимальний набір клієнтів для стабільної роботи з проксі/кукі
        "--extractor-args", "youtube:player_client=android,ios",
    ]
    cookies_path = setup_cookies()
    if cookies_path:
        args += ["--cookies", cookies_path]
    return args


# ── Получение информации о видео ──────────────────────────────────────────────

def get_video_info(url: str) -> dict:
    try:
        # Для отримання інфо НЕ обмежуємо формати, даємо yt-dlp прочитати все доступне
        cmd = ytdlp_base_args() + [
            "--dump-json", 
            "--ignore-no-formats-error", 
            url
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)

        if r.returncode == 0 and r.stdout.strip():
            info = json.loads(r.stdout.strip().split("\n")[0])
            qualities = set()
            for f in info.get("formats", []):
                h = f.get("height")
                vcodec = f.get("vcodec", "none")
                if h and h >= 240 and vcodec != "none":
                    qualities.add(h)
            sorted_q = sorted(qualities, reverse=True)
            return {
                "title":     info.get("title", "Видео"),
                "thumbnail": info.get("thumbnail", ""),
                "duration":  int(info.get("duration") or 0),
                "uploader":  info.get("uploader", ""),
                "platform":  info.get("extractor_key", "").lower(),
                "qualities": [str(q) for q in sorted_q] or ["best"],
            }

        err_type, message = classify_error(r.stderr + r.stdout)
        return {"error": message, "error_type": err_type}

    except subprocess.TimeoutExpired:
        return {"error": "⏱ Таймаут — сайт не ответил", "error_type": "network"}
    except Exception as e:
        return {"error": str(e), "error_type": "unknown"}


# ── Задача скачивания ─────────────────────────────────────────────────────────

def download_task(task_id: str, url: str, quality: str):
    update_task(task_id, status="fetching_info", percent=0,
                step="Получение информации…", created_at=time.time())

    info = get_video_info(url)
    if "error" in info:
        update_task(task_id, status="error",
                    error=info["error"], error_type=info.get("error_type", "unknown"))
        return

    title = info["title"]
    update_task(task_id, title=title, step="Подготовка к загрузке…")
    
    # Очищуємо якість від літер (наприклад, "bestp" -> "", "720p" -> "720")
    clean_q = re.sub(r"\D", "", quality)

    if clean_q and clean_q.isdigit():
        # Максимально всеїдний вибір форматів без прив'язки до розширень. FFmpeg все склеїть самостійно!
        fmt = f"bestvideo[height<={clean_q}]+bestaudio/best[height<={clean_q}]/best"
    else:
        # Режим за замовчуванням (до 720р)
        fmt = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
        
    print(f"[DEBUG] Raw quality from app: '{quality}'")
    print(f"[DEBUG] Cleaned quality: '{clean_q}'")
    print(f"[DEBUG] Selected format string (fmt): '{fmt}'")
    
    safe = re.sub(r"[^\w\sа-яА-Я.-]", "", title)[:60].strip() or "video"
    out = str(TMP_DIR / f"{task_id}_{safe}.%(ext)s")

    cmd = ytdlp_base_args() + [
        "--newline",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-o", out,
        url,
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout_lines = []

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        stdout_lines.append(line)

        if "[download]" in line and "%" in line:
            m_pct   = re.search(r"(\d+\.?\d*)%", line)
            m_speed = re.search(r"at\s+([\d.]+\s*\w+/s)", line)
            m_eta   = re.search(r"ETA\s+([\d:]+)", line)
            m_size  = re.search(r"of\s+([\d.]+\s*\w+)", line)

            if m_pct:
                update_task(task_id, percent=min(95, float(m_pct.group(1))),
                            status="downloading")
            parts = []
            if m_size:  parts.append(f"Размер: {m_size.group(1)}")
            if m_speed: parts.append(f"Скорость: {m_speed.group(1)}")
            if m_eta:   parts.append(f"Осталось: {m_eta.group(1)}")
            if parts:   update_task(task_id, step=" · ".join(parts))

        elif "[Merger]" in line or "Merging" in line:
            update_task(task_id, step="🔀 Объединение аудио и видео…", percent=97)
        elif "Destination:" in line:
            update_task(task_id, step="💾 Сохранение…")

    stderr_out = proc.stderr.read()
    proc.wait()

    if proc.returncode != 0:
        err_type, message = classify_error(stderr_out + "\n".join(stdout_lines))
        update_task(task_id, status="error", error=message, error_type=err_type,
                    error_detail=stderr_out[-500:])
        return

    files = list(TMP_DIR.glob(f"{task_id}_*"))
    if not files:
        update_task(task_id, status="error",
                    error="⚠️ Файл не создан", error_type="unknown")
        return

    vp = max(files, key=lambda f: f.stat().st_size)
    update_task(task_id, status="done", percent=100, step="✓ Готово!",
                file_path=str(vp), filename=vp.name, file_size=vp.stat().st_size)


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    try:
        r = subprocess.run(["yt-dlp", "--version"],
                           capture_output=True, text=True, timeout=5)
        ytdlp_ver = r.stdout.strip()
    except Exception:
        ytdlp_ver = "not found"
    has_cookies = COOKIES_FILE.exists()
    return jsonify({"ok": True, "yt_dlp_version": ytdlp_ver,
                    "cookies": has_cookies,
                    "tasks": len(load_tasks())})


@app.post("/api/info")
def api_info():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "❌ URL не указан", "error_type": "validation"}), 400
    result = get_video_info(url)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@app.post("/api/download")
def api_download():
    body = request.json or {}
    url     = body.get("url", "").strip()
    quality = body.get("quality", "best").strip()
    if not url:
        return jsonify({"error": "❌ URL не указан", "error_type": "validation"}), 400
    task_id = uuid.uuid4().hex[:12]
    set_task(task_id, {"status": "queued", "percent": 0, "created_at": time.time()})
    threading.Thread(target=download_task, args=(task_id, url, quality), daemon=True).start()
    return jsonify({"task_id": task_id})


@app.get("/api/progress/<task_id>")
def api_progress(task_id):
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "Задача не найдена", "error_type": "not_found"}), 404
    return jsonify({k: v for k, v in task.items() if k != "file_path"})


@app.get("/api/file/<task_id>")
def api_file(task_id):
    task = get_task(task_id)
    if not task or task.get("status") != "done":
        abort(404)
    fp = task.get("file_path")
    if not fp or not Path(fp).exists():
        abort(404)
    return send_file(fp, mimetype="video/mp4", as_attachment=True,
                     download_name=task.get("filename", "video.mp4"))


if __name__ == "__main__":
    setup_cookies()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
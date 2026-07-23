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

# Кеш результатов /api/info — чтобы /api/download не делал повторный
# запрос к YouTube для того же URL (это удваивало риск сбоя и выглядело
# для YouTube как подозрительная повторная активность).
_info_cache: dict[str, tuple[float, dict, int]] = {}  # url -> (ts, info, working_client_index)
_info_cache_lock = threading.Lock()
INFO_CACHE_TTL = 600  # 10 минут


# ── Хранение задач на диске ───────────────────────────────────────────────────
# Кеш в памяти намеренно убран — при перезапуске сервера (обновление yt-dlp)
# задачи должны читаться с диска, а не теряться из памяти.

def load_tasks() -> dict:
    try:
        if TASKS_FILE.exists():
            return json.loads(TASKS_FILE.read_text())
    except Exception:
        pass
    return {}

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
        save_tasks(tasks)

def update_task(task_id: str, **kw):
    with _tasks_lock:
        tasks = load_tasks()
        if task_id in tasks:
            tasks[task_id].update(kw)
            # Всегда сохраняем на диск — это медленнее но надёжнее
            save_tasks(tasks)


# ── Cookies из env переменной ─────────────────────────────────────────────────

def setup_cookies() -> str | None:
    """
    Записывает cookies.txt из env и возвращает путь к нему.
    Запись атомарная (через временный файл + rename) — чтобы параллельный
    запрос не прочитал файл в момент его перезаписи (могло давать
    'Unsupported URL' из-за повреждённого cookies.txt).
    """
    b64 = os.environ.get("YT_COOKIES_B64", "").strip()
    if not b64:
        return None

    # Если файл уже записан и не пустой — не перезаписываем его каждый раз,
    # это устраняет гонку между параллельными запросами.
    if COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 100:
        return str(COOKIES_FILE)

    try:
        decoded = base64.b64decode(b64).decode("utf-8")
        tmp_path = COOKIES_FILE.with_suffix(".tmp")
        tmp_path.write_text(decoded)
        tmp_path.replace(COOKIES_FILE)  # атомарная операция на уровне ОС
        print(f"[cookies] Written, size={COOKIES_FILE.stat().st_size} bytes")
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
     "bot", "🤖 YouTube требует авторизацию — нужно настроить cookies"),
    (["failed to extract any player response", "player response"],
     "player", "⚠️ YouTube изменил защиту. Сервер обновляет yt-dlp, попробуйте через 1 минуту."),
    (["requested format is not available"],
     "format", "⚠️ Формат недоступен — попробуйте другое качество или нажмите Скачать ещё раз"),
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

# Каждая попытка — (клиент(ы), использовать_cookies).
# Если cookies просрочены/повреждены, YouTube может отдавать урезанные
# данные ДАЖЕ ПРИ returncode==0 (без явной ошибки) — поэтому у нас есть
# варианты без cookies как fallback, а не только разные клиенты.
CLIENT_ATTEMPTS: list[tuple[str, bool]] = [
    # ВАЖНО: проверено в исходниках yt-dlp (yt_dlp/extractor/youtube/_base.py,
    # INNERTUBE_CLIENTS) — какие клиенты вообще поддерживают cookies:
    #   SUPPORTS_COOKIES=True:  web, web_safari, web_embedded, tv, tv_downgraded
    #   SUPPORTS_COOKIES=False: ios, android, mweb, tv_simply, web_creator
    #
    # ОБНОВЛЕНИЕ: локальный тест yt-dlp с текущими cookies дал явное
    # предупреждение: "The provided YouTube account cookies are no longer
    # valid. They have likely been rotated in the browser as a security
    # measure." — Google аннулирует cookies личного аккаунта при
    # использовании с другого IP (сервер), считая это угоном сессии.
    # Поэтому cookies сейчас НЕ помогают, а вредят — комбинация
    # "протухшая сессия + IP дата-центра" выглядит подозрительнее чем
    # анонимный запрос. Анонимные клиенты (без cookies) теперь идут первыми.
    # Deno установлен и подтверждён (>= 2.3.0 minimum) — расшифровка подписей
    # должна работать сама по себе для большинства обычных публичных видео.
    ("ios", False),
    ("android", False),
    ("mweb", False),
    ("web", False),
    ("tv", False),
    # Cookies оставлены в самом конце — вдруг когда-нибудь будут обновлены
    # свежими. Пока рабочего эффекта от них ждать не стоит.
    ("web", True),
    ("tv", True),
]

def ytdlp_base_args(attempt_index: int = 0) -> list[str]:
    clients, use_cookies = CLIENT_ATTEMPTS[attempt_index % len(CLIENT_ATTEMPTS)]
    args = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--extractor-args", f"youtube:player_client={clients}",
    ]
    if use_cookies:
        cookies_path = setup_cookies()
        if cookies_path:
            args += ["--cookies", cookies_path]
    return args


# ── Получение информации о видео ──────────────────────────────────────────────

def get_video_info(url: str, use_cache: bool = True) -> dict:
    # ── Проверяем кеш ────────────────────────────────────────────────────
    if use_cache:
        with _info_cache_lock:
            cached = _info_cache.get(url)
            if cached and (time.time() - cached[0]) < INFO_CACHE_TTL:
                print(f"[DEBUG] Info cache HIT for {url[:50]} (client_index={cached[2]})")
                result = dict(cached[1])
                result["_client_index"] = cached[2]
                return result

    result, working_client = _fetch_video_info_uncached(url)

    if "error" not in result:
        with _info_cache_lock:
            _info_cache[url] = (time.time(), result, working_client)
        result = dict(result)
        result["_client_index"] = working_client

    return result


def get_cached_client_index(url: str) -> int:
    """Возвращает индекс клиента который сработал для этого URL, или 0."""
    with _info_cache_lock:
        cached = _info_cache.get(url)
        return cached[2] if cached else 0


def _fetch_video_info_uncached(url: str, client_index: int = 0, attempts_left: int | None = None) -> tuple[dict, int]:
    if attempts_left is None:
        attempts_left = len(CLIENT_ATTEMPTS)
    try:
        cmd = ytdlp_base_args(client_index) + [
            "--dump-json",
            "--ignore-no-formats-error",
            url
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)

        if r.returncode == 0 and r.stdout.strip():
            info = json.loads(r.stdout.strip().split("\n")[0])
            raw_formats = info.get("formats", [])
            qualities = set()
            for f in raw_formats:
                h = f.get("height")
                vcodec = f.get("vcodec", "none")
                if h and h >= 240 and vcodec != "none":
                    qualities.add(h)
            sorted_q = sorted(qualities, reverse=True)
            duration = int(info.get("duration") or 0)

            # Диагностика: сколько форматов пришло вообще (даже до фильтрации)
            print(f"[DEBUG] attempt[{client_index}]={CLIENT_ATTEMPTS[client_index % len(CLIENT_ATTEMPTS)]} "
                  f"raw_formats_count={len(raw_formats)} filtered_qualities={sorted_q} duration={duration}")

            is_poor_data = not sorted_q and duration == 0
            if is_poor_data:
                # Печатаем ПОЧЕМУ именно нет форматов — вместо гадания.
                # Эти поля прямо говорят о причине: возрастное ограничение,
                # региональная блокировка, стрим, премьера, платный контент и т.д.
                print(f"[DEBUG] Poor-data diagnostics for this video:")
                print(f"[DEBUG]   age_limit: {info.get('age_limit')}")
                print(f"[DEBUG]   availability: {info.get('availability')}")
                print(f"[DEBUG]   is_live: {info.get('is_live')}")
                print(f"[DEBUG]   live_status: {info.get('live_status')}")
                print(f"[DEBUG]   requires_premium: {info.get('requires_premium')}")
                print(f"[DEBUG]   playable_in_embed: {info.get('playable_in_embed')}")

            # Клиент ответил, но реальных данных почти нет (0 форматов, 0 длительность).
            # Может быть из-за YouTube Shorts С определёнными клиентами, ИЛИ из-за
            # просроченных/повреждённых cookies (см. CLIENT_ATTEMPTS — там есть
            # варианты и с cookies, и без). Считаем неудачей и пробуем следующую
            # комбинацию если попытки ещё остались.
            if is_poor_data and attempts_left > 1:
                print(f"[DEBUG] Poor data (no formats, duration=0) — trying next attempt")
                time.sleep(1.0)
                return _fetch_video_info_uncached(url, client_index + 1, attempts_left - 1)

            print(f"[DEBUG] Info OK with attempt[{client_index}]={CLIENT_ATTEMPTS[client_index % len(CLIENT_ATTEMPTS)]} "
                  f"qualities={sorted_q} duration={duration}")
            return {
                "title":     info.get("title", "Видео"),
                "thumbnail": info.get("thumbnail", ""),
                "duration":  duration,
                "uploader":  info.get("uploader", ""),
                "platform":  info.get("extractor_key", "").lower(),
                "qualities": [str(q) for q in sorted_q] or ["best"],
            }, client_index

        print(f"[DEBUG] yt-dlp FAILED attempt[{client_index}]={CLIENT_ATTEMPTS[client_index % len(CLIENT_ATTEMPTS)]}")
        print(f"[DEBUG] stderr: {r.stderr[-800:]}")
        print(f"[DEBUG] stdout: {r.stdout[-300:]}")

        err_type, message = classify_error(r.stderr + r.stdout)

        if attempts_left > 1:
            print(f"[DEBUG] Trying next client set, {attempts_left - 1} attempts left...")
            time.sleep(1.5)
            return _fetch_video_info_uncached(url, client_index + 1, attempts_left - 1)

        return {"error": message, "error_type": err_type}, client_index

    except subprocess.TimeoutExpired:
        if attempts_left > 1:
            time.sleep(1.5)
            return _fetch_video_info_uncached(url, client_index + 1, attempts_left - 1)
        return {"error": "⏱ Таймаут — сайт не ответил", "error_type": "network"}, client_index
    except Exception as e:
        return {"error": str(e), "error_type": "unknown"}, client_index


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
    # Клиент который сработал для получения инфы — используем его же для скачивания.
    # Раньше здесь всегда брался клиент по умолчанию (индекс 0), даже если
    # get_video_info нашёл рабочий вариант через другой клиент — из-за этого
    # инфо получалось, а скачивание падало с "Unsupported URL".
    working_client_index = info.get("_client_index", get_cached_client_index(url))
    update_task(task_id, title=title, step="Подготовка к загрузке…")

    clean_q = re.sub(r"\D", "", quality)

    def build_format_args(h: int | None) -> list[str]:
        if h:
            return [
                "--format-sort", f"res:{h},ext:mp4:m4a,+codec:avc:m4a",
                "-f", f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best",
            ]
        return [
            "--format-sort", "res,ext:mp4:m4a,+codec:avc:m4a",
            "-f", "bestvideo+bestaudio/best",
        ]

    height = int(clean_q) if clean_q and clean_q.isdigit() else None
    safe = re.sub(r"[^\w\sа-яА-Я.-]", "", title)[:60].strip() or "video"
    out = str(TMP_DIR / f"{task_id}_{safe}.%(ext)s")

    # Пробуем скачать, начиная с клиента который сработал для инфы.
    # Если он вдруг не сработает при скачивании — перебираем остальных.
    max_attempts = len(CLIENT_ATTEMPTS)
    last_stderr = ""
    last_stdout_lines: list[str] = []

    for attempt in range(max_attempts):
        client_index = (working_client_index + attempt) % len(CLIENT_ATTEMPTS)
        extra_args = build_format_args(height)

        print(f"[DEBUG] Download attempt {attempt+1}/{max_attempts} "
              f"attempt[{client_index}]={CLIENT_ATTEMPTS[client_index]} extra_args={extra_args}")

        cmd = ytdlp_base_args(client_index) + extra_args + [
            "--newline",
            "--merge-output-format", "mp4",
            "--ignore-no-formats-error",
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

        if proc.returncode == 0:
            files = list(TMP_DIR.glob(f"{task_id}_*"))
            if files:
                vp = max(files, key=lambda f: f.stat().st_size)
                update_task(task_id, status="done", percent=100, step="✓ Готово!",
                            file_path=str(vp), filename=vp.name, file_size=vp.stat().st_size)
                return
            # returncode 0 но файла нет — считаем неудачей и пробуем следующего клиента
            last_stderr = "Файл не был создан после успешного завершения yt-dlp"
            last_stdout_lines = stdout_lines
        else:
            last_stderr = stderr_out
            last_stdout_lines = stdout_lines
            print(f"[DEBUG] Download attempt {attempt+1} FAILED (attempt_index[{client_index}]={CLIENT_ATTEMPTS[client_index]})")
            print(f"[DEBUG] stderr: {stderr_out[-800:]}")

        # Если ошибка явно не про клиента (например, приватное видео, авторские права) —
        # нет смысла пробовать другие клиенты, сразу выходим
        err_type, _ = classify_error(stderr_out + "\n".join(stdout_lines))
        if err_type in ("private", "age_restricted", "geo_blocked", "not_found", "copyright"):
            break

        # Иначе пробуем следующий набор клиентов
        if attempt < max_attempts - 1:
            time.sleep(1.5)

    # Все попытки исчерпаны
    err_type, message = classify_error(last_stderr + "\n".join(last_stdout_lines))
    update_task(task_id, status="error", error=message, error_type=err_type,
                error_detail=last_stderr[-500:])


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    try:
        r = subprocess.run(["yt-dlp", "--version"],
                           capture_output=True, text=True, timeout=5)
        ytdlp_ver = r.stdout.strip()
    except Exception:
        ytdlp_ver = "not found"

    try:
        r = subprocess.run(["deno", "--version"],
                           capture_output=True, text=True, timeout=5)
        deno_ver = r.stdout.strip().split("\n")[0] if r.returncode == 0 else "ERROR"
    except Exception:
        deno_ver = "NOT FOUND — YouTube extraction will fail!"

    has_cookies = COOKIES_FILE.exists()
    return jsonify({"ok": True, "yt_dlp_version": ytdlp_ver,
                    "deno_version": deno_ver,
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
    # _client_index — служебное поле, не отдаём его в API-ответе
    public_result = {k: v for k, v in result.items() if not k.startswith("_")}
    return jsonify(public_result)


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
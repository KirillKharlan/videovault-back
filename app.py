"""
VideoVault Backend
Fixes:
  1. YouTube bot detection → cookies передаются через env переменную
  2. "Задача не найдена" → задачи хранятся в файле на диске (переживают sleep)
"""

import os, sys, json, threading, subprocess, time, uuid, tempfile, re, base64, queue
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Режим подробной диагностики ─────────────────────────────────────────────
# Включается переменной окружения DEBUG_VERBOSE=1 на Render (Environment →
# добавить переменную, затем redeploy). Добавляет --verbose к yt-dlp и
# печатает значительно больше stderr/stdout в логи — видно, на каком именно
# шаге падает извлечение (player response, nsig/PO token, парсинг форматов
# и т.д.), а не только финальное "No video formats found!".
# ВАЖНО: после диагностики стоит выключить (убрать переменную или поставить
# 0) — verbose режим сильно шумит в логах и чуть медленнее.
DEBUG_VERBOSE = os.environ.get("DEBUG_VERBOSE", "").strip().lower() in ("1", "true", "yes")
_LOG_TAIL_CHARS = 6000 if DEBUG_VERBOSE else 800
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

# yt_dlp как библиотека (не CLI-бинарь) — запускается ВСЕГДА как отдельный
# подпроцесс через sys.executable (см. комментарий в самом ytdlp_worker.py
# про то, зачем нужна изоляция подпроцессом).
WORKER_SCRIPT = BASE_DIR / "ytdlp_worker.py"

# Файл для хранения задач — переживает sleep сервера
TASKS_FILE = BASE_DIR / "tasks.json"
COOKIES_FILE = BASE_DIR / "cookies.txt"
_tasks_lock = threading.Lock()

# Кеш результатов /api/info — чтобы /api/download мог сразу знать, какой
# клиент недавно сработал для этого же URL, и какие форматы/качества
# доступны (используется при выборе точного format_id на скачивании —
# см. _resolve_format_ids). Сам файл видео всё равно извлекается заново на
# каждой попытке скачивания (переиспользование готовых ссылок на файл
# оказалось ненадёжным — см. историю в ytdlp_worker.py).
# url -> (ts, summary_dict, working_client_index, raw_info_dict, proxy_used)
_info_cache: dict[str, tuple[float, dict, int, dict, str | None]] = {}
_info_cache_lock = threading.Lock()
INFO_CACHE_TTL = 600  # 10 минут

# ── Глобальный "последний рабочий клиент" ───────────────────────────────────
# Раньше каждый новый запрос (для НОВОГО, ещё не кешированного URL) всегда
# начинал перебор с CLIENT_ATTEMPTS[0], даже если для 50 предыдущих видео
# подряд отвечал только, скажем, клиент №2. Теперь запоминаем глобально,
# какой клиент сработал последним (по любому URL), и новые запросы стартуют
# именно с него — по сути self-tuning под то, что сейчас реально пропускает
# YouTube/прокси, без ручной перестановки CLIENT_ATTEMPTS.
_global_client_lock = threading.Lock()
_global_best_client_index = 0

def _record_client_success(idx: int) -> None:
    global _global_best_client_index
    with _global_client_lock:
        _global_best_client_index = idx % len(CLIENT_ATTEMPTS)

def _get_global_best_client_index() -> int:
    with _global_client_lock:
        return _global_best_client_index

# Если от попытки скачивания не пришло ни строчки прогресса дольше этого —
# считаем её зависшей (плохой прокси/сеть) и переходим к следующей попытке,
# вместо того чтобы ждать бесконечно (раньше тут тайм-аута не было вообще).
STALL_TIMEOUT = 90  # секунд


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


def reconcile_stale_tasks():
    """
    Вызывается один раз при старте процесса (импорт модуля — значит,
    сработает и при полном рестарте, И при том, что gunicorn arbiter
    молча респавнит воркер после его смерти, например от OOM).

    Любая задача, оставшаяся в незавершённом статусе (queued/fetching_info/
    downloading), физически не может быть продолжена — процесс, который её
    выполнял, больше не существует. Раньше в этом случае /api/progress
    бесконечно отдавал старый статус "downloading 43%", и приложение
    зависало без объяснений. Теперь сразу помечаем такие задачи ошибкой
    с понятной причиной.
    """
    with _tasks_lock:
        tasks = load_tasks()
        changed = False
        for tid, t in tasks.items():
            if t.get("status") in ("queued", "fetching_info", "downloading"):
                t["status"] = "error"
                t["error"] = "🔄 Сервер перезапустился во время загрузки — нажмите «Скачать» ещё раз"
                t["error_type"] = "server_restart"
                changed = True
        if changed:
            print(f"[startup] Reconciled {sum(1 for t in tasks.values() if t.get('error_type') == 'server_restart')} stale task(s)")
            save_tasks(tasks)

reconcile_stale_tasks()


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
    (["cookies are no longer valid", "no longer valid", "cookies have expired",
      "cookies are invalid"],
     "cookies_expired",
     "🍪 Куки для YouTube протухли/отозваны — нужно переэкспортировать "
     "свежие из залогиненного браузера и обновить YT_COOKIES_B64"),
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
    (["402 payment required", "payment required", "tunnel connection failed: 402"],
     "proxy_quota", "💳 У прокси-аккаунта закончился лимит трафика"),
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

# ── Лестница качества для fallback при OOM ────────────────────────────────────
# Free-тариф Render — 512MB RAM. Если yt-dlp/ffmpeg убиты сигналом (похоже на
# OOM), помимо смены клиента имеет смысл ещё и понизить целевое разрешение —
# это не даёт гарантии, но заметно снижает пиковую память на слиянии
# видео+аудио для тяжёлых (4K/1440p) исходников.
QUALITY_LADDER = [2160, 1440, 1080, 720, 480, 360, 240]

def _next_lower_quality(h: int | None) -> int | None:
    """Следующая по списку более низкая ступень качества, либо None если
    дальше понижать некуда (h уже минимальный)."""
    if h is None:
        # "best" может резолвиться в 4K/8K — сразу ставим потолок 1080p
        return 1080
    for q in QUALITY_LADDER:
        if q < h:
            return q
    return None

# ── Пул прокси ──────────────────────────────────────────────────────────────
# Поддержка нескольких прокси-аккаунтов (например, 3 бесплатных Webshare
# аккаунта по ~1GB каждый = суммарно больше трафика).
#
# Настройка на Render — переменная окружения YT_PROXIES, через запятую:
#   YT_PROXIES=http://user1:pass1@ip1:port1,http://user2:pass2@ip2:port2,http://user3:pass3@ip3:port3
#
# Обратная совместимость: если YT_PROXIES не задана, но задана старая
# одиночная YT_PROXY — используется она одна.
def _load_proxy_pool() -> list[str]:
    multi = os.environ.get("YT_PROXIES", "").strip()
    if multi:
        return [p.strip() for p in multi.split(",") if p.strip()]
    single = os.environ.get("YT_PROXY", "").strip()
    return [single] if single else []

PROXY_POOL: list[str] = _load_proxy_pool()


def _proxy_account(proxy_url: str) -> str:
    """Достаём 'аккаунт' прокси из URL — это username в http://user:pass@host:port.
    У нас несколько IP делят один и тот же аккаунт/лимит трафика (например
    3 прокси-провайдера по несколько IP каждый), так что 402 на одном IP
    аккаунта означает, что лимит исчерпан у ВСЕХ его IP."""
    try:
        creds = proxy_url.split("://", 1)[1].split("@", 1)[0]
        return creds.split(":", 1)[0]
    except (IndexError, ValueError):
        return proxy_url  # fallback — считаем весь URL уникальным "аккаунтом"


# ── Учёт истощённых прокси-аккаунтов (402 Payment Required) ────────────────
# Лимиты трафика у бесплатных прокси-провайдеров обычно обнуляются раз в
# месяц. Храним время, когда аккаунт был помечен как исчерпанный, и просто
# перестаём его предлагать на PROXY_DEAD_COOLDOWN_SECONDS — вместо того чтобы
# тратить retry-попытки на заведомо мёртвые IP этого аккаунта.
PROXY_DEAD_COOLDOWN_SECONDS = 6 * 3600  # 6 часов; на 402 быстро не воскресает
_dead_proxy_accounts: dict[str, float] = {}
_dead_proxy_lock = threading.Lock()


def mark_proxy_account_dead(proxy_url: str) -> None:
    account = _proxy_account(proxy_url)
    with _dead_proxy_lock:
        already_dead = account in _dead_proxy_accounts
        _dead_proxy_accounts[account] = time.time()
    if not already_dead:
        print(f"[DEBUG] Proxy account '{account}' помечен как исчерпанный (402) — "
              f"пропускаем на {PROXY_DEAD_COOLDOWN_SECONDS // 3600}ч")


def _is_account_dead(account: str) -> bool:
    with _dead_proxy_lock:
        died_at = _dead_proxy_accounts.get(account)
    if died_at is None:
        return False
    if time.time() - died_at > PROXY_DEAD_COOLDOWN_SECONDS:
        # Cooldown истёк — даём аккаунту ещё один шанс.
        with _dead_proxy_lock:
            _dead_proxy_accounts.pop(account, None)
        return False
    return True


# ── Независимая ротация прокси ──────────────────────────────────────────────
# Раньше индекс прокси совпадал с индексом клиента (attempt_index), а этот
# индекс каждый раз стартовал с 0 для НОВОГО видео. При 12 прокси и 7
# клиентах (CLIENT_ATTEMPTS) это значило, что прокси с позициями 7-11 в
# пуле не использовались вообще НИКОГДА, ни для одного запроса — не в
# рамках одной попытки, а вообще, потому что счётчик не переживал между
# запросами. Чтобы весь пул реально работал, ротацию прокси делаем сквозной:
# глобальный счётчик увеличивается на каждый вызов select_proxy(), не
# сбрасываясь между разными видео/запросами. Так за несколько запросов
# гарантированно проходим по всем прокси пула по кругу, а не только по
# первым len(CLIENT_ATTEMPTS) из них.
_proxy_rotation_index = 0
_proxy_rotation_lock = threading.Lock()


def _next_proxy_start_index() -> int:
    global _proxy_rotation_index
    with _proxy_rotation_lock:
        idx = _proxy_rotation_index
        _proxy_rotation_index += 1
    return idx


def select_proxy() -> str | None:
    """Выбирает следующий прокси по сквозной ротации (независимо от того,
    какой клиент/попытка сейчас перебирается), пропуская аккаунты с
    недавним 402 (исчерпанный лимит). Если все аккаунты мертвы — best-effort:
    всё равно возвращает прокси по кругу (вдруг классификация ошиблась)."""
    if not PROXY_POOL:
        return None
    n = len(PROXY_POOL)
    start = _next_proxy_start_index()
    for i in range(n):
        candidate = PROXY_POOL[(start + i) % n]
        if not _is_account_dead(_proxy_account(candidate)):
            return candidate
    # Все аккаунты мертвы — не блокируем работу совсем, пробуем как есть.
    return PROXY_POOL[start % n]


# ── Получение информации о видео ──────────────────────────────────────────────

# ── Точный подбор format_id из закешированной инфы ──────────────────────────
# В отличие от прошлой (откаченной) идеи "переиспользовать готовые ссылки на
# файл", здесь переиспользуется только СПИСОК форматов (какие itag'и вообще
# существуют для этого видео) — не сами ссылки. На скачивании всё равно
# происходит полное свежее извлечение, просто yt-dlp сразу знает точный
# format_id вместо того чтобы каждый раз заново перебирать/угадывать его по
# generic-селектору вида "bestvideo[height<=X]". Generic-селектор всё равно
# остаётся как fallback внутри итогового селектора (через "/") — если
# конкретный id вдруг не найдётся на свежем извлечении (например, YouTube
# изменил набор дорожек), скачивание не упадёт, просто отработает как раньше.
def _best_video_format_id(formats: list[dict], height: int | None) -> str | None:
    candidates = [f for f in formats
                  if f.get("vcodec", "none") != "none" and f.get("acodec", "none") == "none"
                  and f.get("format_id")]
    if not candidates:
        return None
    if height:
        within = [f for f in candidates if (f.get("height") or 0) <= height]
        if within:
            candidates = within
    def score(f):
        is_mp4_avc = 1 if (f.get("ext") == "mp4" or "avc" in (f.get("vcodec") or "")) else 0
        return (is_mp4_avc, f.get("height") or 0, f.get("tbr") or 0)
    return str(max(candidates, key=score)["format_id"])


def _best_audio_format_id(formats: list[dict]) -> str | None:
    candidates = [f for f in formats
                  if f.get("acodec", "none") != "none" and f.get("vcodec", "none") == "none"
                  and f.get("format_id")]
    if not candidates:
        return None
    def score(f):
        is_m4a = 1 if f.get("ext") == "m4a" else 0
        return (is_m4a, f.get("abr") or 0)
    return str(max(candidates, key=score)["format_id"])


def _resolve_format_selector(raw_info: dict | None, height: int | None, is_audio_only: bool) -> str:
    """Возвращает format-селектор для yt-dlp: точный format_id из
    закешированной инфы + generic-селектор как fallback. Если кэша/форматов
    нет — просто generic-селектор, ничего не меняется относительно старого
    поведения."""
    generic = ("bestaudio/best" if is_audio_only else (
        f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best" if height
        else "bestvideo+bestaudio/best"
    ))
    formats = (raw_info or {}).get("formats") or []
    if not formats:
        return generic

    if is_audio_only:
        audio_id = _best_audio_format_id(formats)
        return f"{audio_id}/{generic}" if audio_id else generic

    video_id = _best_video_format_id(formats, height)
    audio_id = _best_audio_format_id(formats)
    if video_id and audio_id:
        return f"{video_id}+{audio_id}/{generic}"
    return generic


def get_cached_raw_info(url: str) -> dict | None:
    """"Сырой" info dict (со списком форматов) для этого URL, если он ещё в
    кеше — используется ТОЛЬКО для подбора точного format_id
    (_resolve_format_selector), не для переиспользования ссылок на файл."""
    with _info_cache_lock:
        cached = _info_cache.get(url)
        if cached and (time.time() - cached[0]) < INFO_CACHE_TTL:
            return cached[3]
    return None


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

    result, working_client, raw_info, proxy_used = _fetch_video_info_uncached(url)

    if "error" not in result:
        with _info_cache_lock:
            _info_cache[url] = (time.time(), result, working_client, raw_info, proxy_used)
        result = dict(result)
        result["_client_index"] = working_client

    return result


def get_cached_client_index(url: str) -> int:
    """Индекс клиента, который сработал для ЭТОГО URL, если он ещё в кеше;
    иначе — глобально последний рабочий клиент (не всегда 0)."""
    with _info_cache_lock:
        cached = _info_cache.get(url)
        if cached:
            return cached[2]
    return _get_global_best_client_index()


def _run_worker_once(cfg: dict, timeout: int) -> tuple[int, str, str]:
    """Разовый вызов ytdlp_worker.py — для случаев, когда нужно записать
    конфиг, дождаться завершения и прочитать весь вывод целиком (без
    построчного чтения по ходу выполнения). Всё это делает сам
    communicate(input=...) за один безопасный проход — не трогаем pipe'ы
    вручную, чтобы не словить 'I/O operation on closed file' (двойное
    ручное закрытие stdin конфликтовало с внутренней логикой communicate())."""
    proc = subprocess.Popen(
        [sys.executable, str(WORKER_SCRIPT)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout_data, stderr_data = proc.communicate(
            input=json.dumps(cfg, ensure_ascii=False), timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()  # добираем пайпы после kill, чтобы не оставить зомби-процесс
        raise
    return proc.returncode, stdout_data, stderr_data


def _run_worker_streaming(cfg: dict) -> subprocess.Popen:
    """Потоковый вызов ytdlp_worker.py — для скачивания, где нужно читать
    stdout построчно ПО ХОДУ выполнения (прогресс), поэтому communicate()
    здесь не подходит (он ждёт завершения процесса целиком). Конфиг пишем и
    сразу закрываем stdin вручную — это единственное место, где стдин вообще
    трогается для этого proc, так что второго закрытия конфликтовать не с
    чем."""
    proc = subprocess.Popen(
        [sys.executable, str(WORKER_SCRIPT)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    proc.stdin.write(json.dumps(cfg, ensure_ascii=False))
    proc.stdin.close()
    return proc


def _try_one_client_for_info(url: str, client_index: int) -> dict:
    """Одна попытка получить инфо через ОДИН конкретный клиент — вынесена в
    отдельную функцию, чтобы запускать несколько таких попыток ОДНОВРЕМЕННО
    (см. _fetch_video_info_uncached) вместо строго последовательного
    перебора. Возвращает dict с "ok": True/False и подробностями."""
    clients, use_cookies = CLIENT_ATTEMPTS[client_index % len(CLIENT_ATTEMPTS)]
    proxy_used = select_proxy()
    cookies_path = setup_cookies() if use_cookies else None
    cfg = {
        "mode": "info", "url": url, "client": clients,
        "use_cookies": use_cookies, "cookies_path": cookies_path,
        "proxy": proxy_used, "verbose": DEBUG_VERBOSE,
    }
    label = f"attempt[{client_index}]={CLIENT_ATTEMPTS[client_index % len(CLIENT_ATTEMPTS)]}"

    try:
        proc_returncode, stdout_data, stderr_data = _run_worker_once(cfg, timeout=45)
    except subprocess.TimeoutExpired:
        return {"ok": False, "client_index": client_index, "proxy_used": proxy_used,
                "err_type": "network", "message": "⏱ Таймаут — сайт не ответил"}
    except Exception as e:
        return {"ok": False, "client_index": client_index, "proxy_used": None,
                "err_type": "unknown", "message": str(e)}

    if proc_returncode == 0 and stdout_data.strip():
        event = json.loads(stdout_data.strip().split("\n")[0])
        info = event.get("info", {})
        raw_formats = info.get("formats") or []
        qualities = set()
        for f in raw_formats:
            h = f.get("height")
            vcodec = f.get("vcodec", "none")
            if h and h >= 240 and vcodec != "none":
                qualities.add(h)
        sorted_q = sorted(qualities, reverse=True)
        duration = int(info.get("duration") or 0)

        # Диагностика: сколько форматов пришло вообще (даже до фильтрации)
        print(f"[DEBUG] {label} proxy={(proxy_used or 'none')[:30]}... "
              f"raw_formats_count={len(raw_formats)} filtered_qualities={sorted_q} duration={duration}")

        is_poor_data = not sorted_q and duration == 0
        if is_poor_data:
            # Печатаем ПОЧЕМУ именно нет форматов — вместо гадания. Эти поля
            # прямо говорят о причине: возрастное ограничение, региональная
            # блокировка, стрим, премьера, платный контент и т.д.
            print(f"[DEBUG] Poor-data diagnostics for this video:")
            print(f"[DEBUG]   age_limit: {info.get('age_limit')}")
            print(f"[DEBUG]   availability: {info.get('availability')}")
            print(f"[DEBUG]   is_live: {info.get('is_live')}")
            print(f"[DEBUG]   live_status: {info.get('live_status')}")
            print(f"[DEBUG]   requires_premium: {info.get('requires_premium')}")
            print(f"[DEBUG]   playable_in_embed: {info.get('playable_in_embed')}")
            # Раньше stderr воркера печатался ТОЛЬКО при явной ошибке
            # (proc_returncode != 0) — а "пустые данные" технически считаются
            # успехом (returncode 0), поэтому реальный verbose-вывод yt-dlp
            # (где обычно прямо написана причина — "Sign in to confirm..." и
            # т.п.) молча проглатывался. Печатаем его и здесь, раз
            # DEBUG_VERBOSE включён.
            if DEBUG_VERBOSE and stderr_data.strip():
                print(f"[DEBUG] Verbose stderr for {label} (poor data):")
                print(stderr_data[-_LOG_TAIL_CHARS:])
            return {"ok": False, "client_index": client_index, "proxy_used": proxy_used,
                    "err_type": "poor_data", "message": "Пустые данные от этого клиента"}

        print(f"[DEBUG] Info OK with {label} qualities={sorted_q} duration={duration}")
        summary = {
            "title":     info.get("title", "Видео"),
            "thumbnail": info.get("thumbnail", ""),
            "duration":  duration,
            "uploader":  info.get("uploader", ""),
            "platform":  info.get("extractor_key", "").lower(),
            "qualities": [str(q) for q in sorted_q] or ["best"],
        }
        return {"ok": True, "client_index": client_index, "proxy_used": proxy_used,
                "summary": summary, "raw_info": info}

    print(f"[DEBUG] yt-dlp worker FAILED {label}")
    print(f"[DEBUG] stderr: {stderr_data[-_LOG_TAIL_CHARS:]}")
    print(f"[DEBUG] stdout: {stdout_data[-_LOG_TAIL_CHARS:]}")
    err_type, message = classify_error(stderr_data + stdout_data)
    if err_type == "proxy_quota" and proxy_used:
        mark_proxy_account_dead(proxy_used)
    return {"ok": False, "client_index": client_index, "proxy_used": proxy_used,
            "err_type": err_type, "message": message}


# Сколько клиентов пробовать ОДНОВРЕМЕННО за один "залп", вместо строго по
# одному. Компромисс: больше — быстрее приходит первый успешный ответ, но
# больше одновременной нагрузки на прокси-пул сразу. 3 — разумная середина
# при небольшом пуле прокси.
INFO_PARALLEL_BATCH = 3


def _client_order(start_index: int) -> list[int]:
    """Порядок перебора клиентов, начиная с [start_index] и по кругу через
    все остальные — так же, как раньше, только не всегда с нуля."""
    n = len(CLIENT_ATTEMPTS)
    return [(start_index + i) % n for i in range(n)]


def _fetch_video_info_uncached(url: str) -> tuple[dict, int, dict | None, str | None]:
    """Перебирает клиентов ПАРТИЯМИ по INFO_PARALLEL_BATCH штук одновременно
    (вместо строго по одному) — берём первый успешный ответ в партии и сразу
    возвращаем, не дожидаясь остальных. Начинаем не с клиента №0, а с
    глобально последнего рабочего (см. _get_global_best_client_index)."""
    order = _client_order(_get_global_best_client_index())
    last_err_type, last_message = "unknown", "Не удалось получить информацию о видео"

    i = 0
    while i < len(order):
        batch = order[i:i + INFO_PARALLEL_BATCH]
        ex = ThreadPoolExecutor(max_workers=len(batch))
        futures = [ex.submit(_try_one_client_for_info, url, ci) for ci in batch]
        try:
            for fut in as_completed(futures):
                res = fut.result()
                if res["ok"]:
                    _record_client_success(res["client_index"])
                    return res["summary"], res["client_index"], res["raw_info"], res["proxy_used"]
                if res["err_type"] in ("private", "age_restricted", "geo_blocked", "not_found", "copyright"):
                    # Содержательная ошибка про само видео (не про клиента) —
                    # дальше пробовать смысла нет, ни в этой партии, ни в следующих.
                    return ({"error": res["message"], "error_type": res["err_type"]},
                            res["client_index"], None, res["proxy_used"])
                last_err_type, last_message = res["err_type"], res["message"]
        finally:
            # wait=False — не блокируем возврат результата, пока "лишние"
            # (уже не нужные, раз кто-то в партии сработал) потоки в фоне
            # доработают сами по себе и результат отбросится.
            ex.shutdown(wait=False, cancel_futures=True)
        i += INFO_PARALLEL_BATCH
        if i < len(order):
            time.sleep(1.0)

    return {"error": last_message, "error_type": last_err_type}, (order[-1] if order else 0), None, None


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
    is_audio_only = quality.strip().lower() in ("mp3", "audio", "audio_only")

    height = int(clean_q) if clean_q and clean_q.isdigit() else None
    safe = re.sub(r"[^\w\sа-яА-Я.-]", "", title)[:60].strip() or "video"
    out = str(TMP_DIR / f"{task_id}_{safe}.%(ext)s")

    # Точный format_id из закешированной инфы (если она ещё жива) — с
    # generic-селектором как fallback внутри самого себя. См.
    # _resolve_format_selector — это НЕ переиспользование ссылок на файл
    # (та идея была откачена как ненадёжная), а только списка доступных
    # дорожек, что безопасно даже при полностью свежем извлечении.
    exact_format = _resolve_format_selector(get_cached_raw_info(url), height, is_audio_only)

    # Пробуем скачать, начиная с клиента который сработал для инфы.
    # Если он вдруг не сработает при скачивании — перебираем остальных.
    max_attempts = len(CLIENT_ATTEMPTS) * 20
    last_stderr = ""
    last_stdout_lines: list[str] = []
    was_killed_by_signal = False

    for attempt in range(max_attempts):
        client_index = (working_client_index + attempt) % len(CLIENT_ATTEMPTS)
        clients, use_cookies = CLIENT_ATTEMPTS[client_index % len(CLIENT_ATTEMPTS)]
        cookies_path = setup_cookies() if use_cookies else None
        proxy_used = select_proxy()

        print(f"[DEBUG] Download attempt {attempt+1}/{max_attempts} "
              f"attempt[{client_index}]={CLIENT_ATTEMPTS[client_index]} "
              f"height={height} audio={is_audio_only} format={exact_format}")

        cfg = {
            "mode": "download",
            "url": url,
            "client": clients,
            "use_cookies": use_cookies,
            "cookies_path": cookies_path,
            "proxy": proxy_used,
            "verbose": DEBUG_VERBOSE,
            "height": height,
            "is_audio_only": is_audio_only,
            "output_template": out,
            "exact_format": exact_format,
        }

        proc = _run_worker_streaming(cfg)
        stdout_lines: list[str] = []

        # ── Детект "зависшей" попытки ──────────────────────────────────────
        # Раньше здесь было простое blocking-чтение (for line in proc.stdout),
        # без вообще какого-либо тайм-аута — если сеть/прокси застревали,
        # попытка могла висеть бесконечно, а не переходить к следующей.
        # Читаем строки в отдельном потоке через очередь: если новых строк
        # нет дольше STALL_TIMEOUT секунд подряд — считаем попытку зависшей,
        # убиваем процесс и идём к следующей попытке/клиенту.
        line_queue: "queue.Queue[str | None]" = queue.Queue()

        def _reader():
            try:
                for ln in proc.stdout:
                    line_queue.put(ln)
            finally:
                line_queue.put(None)  # сигнал конца потока

        threading.Thread(target=_reader, daemon=True).start()

        stalled = False
        while True:
            try:
                line = line_queue.get(timeout=STALL_TIMEOUT)
            except queue.Empty:
                stalled = True
                print(f"[DEBUG] Attempt {attempt+1} stalled — no progress for "
                      f"{STALL_TIMEOUT}s, killing and moving to next attempt")
                proc.kill()
                break
            if line is None:
                break  # процесс завершился сам — нормальный путь
            line = line.strip()
            if not line:
                continue
            stdout_lines.append(line)
            try:
                event = json.loads(line)
            except ValueError:
                continue  # на всякий случай игнорируем нераспознанные строки

            if event.get("type") == "progress":
                pct = event.get("percent")
                if pct is not None:
                    update_task(task_id, percent=min(95, pct), status="downloading")
                parts = []
                if event.get("total"): parts.append(f"Размер: {event['total']}")
                if event.get("speed"): parts.append(f"Скорость: {event['speed']}")
                if event.get("eta"):   parts.append(f"Осталось: {event['eta']}")
                if parts: update_task(task_id, step=" · ".join(parts))
            elif event.get("type") == "step":
                update_task(task_id, step=event.get("text", ""))

        stderr_out = proc.stderr.read()
        proc.wait()

        if stalled:
            last_stderr = (f"⏱ Загрузка зависла (нет прогресса {STALL_TIMEOUT}s) — "
                            f"вероятно, проблема с прокси/сетью")
            last_stdout_lines = stdout_lines
        elif proc.returncode == 0:
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
            print(f"[DEBUG] stderr: {stderr_out[-_LOG_TAIL_CHARS:]}")

            _err_type, _ = classify_error(stderr_out)
            if _err_type == "proxy_quota" and proxy_used:
                mark_proxy_account_dead(proxy_used)

            # returncode < 0 значит процесс убит сигналом (например SIGKILL от
            # OOM killer), а не завершился с обычной ошибкой yt-dlp. stderr в
            # этом случае почти всегда пустой — classify_error() ничего
            # осмысленного не найдёт. Понижаем целевое качество и пробуем
            # следующую попытку с ним — это не гарантия, но для тяжёлых
            # (4K/1440p) исходников заметно снижает риск повторного OOM.
            if proc.returncode is not None and proc.returncode < 0:
                was_killed_by_signal = True
                lower = _next_lower_quality(height)
                print(f"[DEBUG] Process killed by signal {proc.returncode} — "
                      f"looks like OOM. Lowering quality {height} → {lower}")
                if lower is not None:
                    height = lower
                    update_task(task_id, step=f"⚠️ Не хватило ресурсов — пробуем качество ≤{height}p…")

        # Если ошибка явно не про клиента (например, приватное видео, авторские права) —
        # нет смысла пробовать другие клиенты, сразу выходим
        err_type, _ = classify_error(stderr_out + "\n".join(stdout_lines))
        if err_type in ("private", "age_restricted", "geo_blocked", "not_found", "copyright"):
            break

        # Иначе пробуем следующий набор клиентов
        if attempt < max_attempts - 1:
            time.sleep(1.5)

    # Все попытки исчерпаны
    if was_killed_by_signal and not last_stderr.strip():
        # Все неудачи были сигнальными убийствами процесса (OOM-подобное), а
        # содержательного stderr для classify_error() нет — даём понятное
        # сообщение вместо generic "⚠️ Ошибка: " с пустым текстом.
        err_type, message = "resource", (
            "💾 Серверу не хватило памяти на это видео даже на низком качестве. "
            "Попробуйте ещё раз чуть позже или выберите более короткое видео."
        )
    else:
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
    with _dead_proxy_lock:
        dead_accounts = {
            acc: round(PROXY_DEAD_COOLDOWN_SECONDS - (time.time() - died_at))
            for acc, died_at in _dead_proxy_accounts.items()
            if time.time() - died_at <= PROXY_DEAD_COOLDOWN_SECONDS
        }
    return jsonify({"ok": True, "yt_dlp_version": ytdlp_ver,
                    "deno_version": deno_ver,
                    "cookies": has_cookies,
                    "debug_verbose": DEBUG_VERBOSE,
                    "proxy_count": len(PROXY_POOL),
                    "proxy_rotation_position": _proxy_rotation_index % len(PROXY_POOL) if PROXY_POOL else 0,
                    "proxy_accounts_on_cooldown": dead_accounts,  # {username: seconds_left}
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
    filename = task.get("filename", "video.mp4")
    is_mp3 = filename.lower().endswith(".mp3")
    mimetype = "audio/mpeg" if is_mp3 else "video/mp4"
    return send_file(fp, mimetype=mimetype, as_attachment=True, download_name=filename)


if __name__ == "__main__":
    setup_cookies()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
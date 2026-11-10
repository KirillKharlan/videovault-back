# 🗺️ КАРТА ПРОЕКТА — videovault-backend

> Быстрая ориентация (в том числе для Claude), чтобы не перечитывать код при каждом фиксе.
> Обновляй при структурных изменениях. Последняя сверка с кодом: ноябрь 2026.
> Англоязычный контекст для Claude — `claude/claude.md`.

## Структура

```
videovault-backend/
├── app.py              ← весь backend (Flask, ~1000 строк)
├── ytdlp_worker.py     ← yt-dlp как библиотека, запускается app.py как ПОДПРОЦЕСС
├── start.sh            ← точка входа: обновляет yt-dlp и bgutil-плагин, затем gunicorn
├── Dockerfile          ← python:3.11-slim + ffmpeg + Deno + pinned flask/flask-cors/gunicorn
├── render.yaml         ← два сервиса Render: videovault-server и videovault-pot-provider
├── requirements.txt    ← справочный (см. ниже), Docker-сборка его НЕ использует
├── README_DEPLOY.md    ← инструкция по деплою
└── *cookies*.txt       ← ЛОКАЛЬНЫЕ секреты для получения YT_COOKIES_B64 (на сервер не заливаются)
```

## Архитектура

- `app.py` (gunicorn: 1 воркер, 8 потоков `--threads 8`, 300 с таймаут) **не импортирует yt-dlp**. На каждую операцию он запускает
  `ytdlp_worker.py` отдельным процессом. Если видео убьёт процесс по памяти (OOM), погибнет только воркер:
  родитель увидит `returncode < 0` и включит понижение качества.
- Протокол воркера: конфиг приходит одной JSON-строкой в stdin; результат — JSON-события построчно в stdout
  (`info`, `progress`, `step`, `postprocess`…); ошибки — текст исключения в stderr, его разбирает `classify_error()`.
- Режимы воркера: `info` (метаданные без скачивания), `download` (извлечение + скачивание в файл за один проход,
  фрагменты в 4 потока) и `stream` (байты одиночного mp4 пишутся прямо в stdout, см. «Прямая выдача»). Идея «переиспользовать ссылки из /api/info при скачивании» откатана: ссылки протухают.
  Info и download идут параллельно и независимо.
- Задачи хранятся в `tasks.json` рядом с `app.py` (не в памяти), чтобы переживать рестарт.
- Файлы лежат в `<tmp>/videovault`; фоновый `cleanup_loop` удаляет старые файлы и задачи (старше ~2 ч).

## API

| Маршрут | Назначение |
|---|---|
| `GET /health` | `ok`, версии yt-dlp и Deno, наличие cookies, `debug_verbose`, число прокси, прокси на cooldown, число задач |
| `POST /api/info` `{url, refresh?}` | `title`, `thumbnail`, `duration`, `uploader`, `platform`, `qualities[]` (строки; иначе `["best"]`), `quality_sizes{высота: байты}` (оценка, есть не для всех). `refresh: true` — мимо кэша. Служебные поля с `_` скрыты. Ошибка: 400 `{error, error_type}` |
| `POST /api/fetch` `{url, quality}` | **основной путь клиента**: один запрос — сразу видео. Ответ: `video/mp4` потоком (заголовки `X-File-Size` — оценка, `X-Title` — percent-encoded название) **или** JSON `{"mode":"task","task_id"}` (запасной путь) **или** 400 `{error, error_type}` |
| `POST /api/download` `{url, quality}` | создаёт `task_id`, запускает `download_task` в потоке. `quality`: `"best"`, число (`"720"`) или `mp3`/`audio`/`audio_only` |
| `GET /api/progress/<id>` | поля задачи без `file_path`: `status`, `percent`, `step`, `title`, `filename`, `error`, `error_type`, `error_detail`, `created_at`; 404 `not_found`, если задачи нет |
| `GET /api/file/<id>` | отдаёт файл (`audio/mpeg` для mp3, иначе `video/mp4`), только при `status == done` |

### Прямая выдача (`/api/fetch`)

Работает, только если выполнены ВСЕ условия (`_plan_stream`): есть кэш `/api/info` для ссылки (10 мин), не mp3, и в
списке форматов есть одиночный (видео+аудио) mp4 по http(s), который по высоте не хуже лучшей отдельной видеодорожки
(иначе склейка даст лучшую картинку, например YouTube 720p+). Типичные случаи: TikTok, Instagram, YouTube 360p.

1. `_open_stream` перебирает клиентов и прокси ДО первого байта: до `STREAM_MAX_ATTEMPTS = 3` попыток по
   `STREAM_FIRST_BYTE_TIMEOUT = 45` с. Воркер в режиме `stream` пишет mp4 в stdout, `_StreamSession` вычитывает его
   в ограниченную очередь (противодавление: медленный клиент притормаживает yt-dlp).
2. Получили первый байт: отвечаем `video/mp4` (chunked), `_stream_body` отдаёт данные по мере поступления.
   Воркер умер или завис посреди потока (`STALL_TIMEOUT`) → генератор бросает исключение, соединение рвётся без
   финального chunk, клиент видит ошибку (неполный файл за успешный не принимается).
3. Содержательная ошибка про само видео (приватное, удалено…) → 400 JSON. Любая другая неудача, нет кэша, mp3, нужна
   склейка → ответ `{"mode":"task"}` и работает прежняя схема (`download_task` + `/api/progress` + `/api/file`).
4. Клиент отключился → генератор закрывается, процесс воркера убивается.

Фрагменты в 4 потока и OOM-лесенка качества в прямом режиме не применяются (файл не склеивается и не копится на диске).

Статусы задачи: `queued` → `fetching_info` → `downloading` → `done` | `error`.
Прогресс не доходит до 100 до завершения: во время загрузки `percent ≤ 95`.

## app.py — карта разделов (сверху вниз)

1. Импорты, Flask, CORS (открыт для всех источников), логирование запросов; `DEBUG_VERBOSE` задаёт длину хвоста логов (800 / 6000 символов).
2. Пути: `TMP_DIR`, `WORKER_SCRIPT`, `TASKS_FILE`, `COOKIES_FILE`; `INFO_CACHE_TTL = 600`; `STALL_TIMEOUT = 90`.
3. «Глобально лучший клиент»: `_record_client_success`, `_get_global_best_client_index` — начинать перебор с последнего рабочего.
4. Хранилище задач: `load_tasks`, `save_tasks`, `get_task`, `set_task`, `update_task` (всегда читают/пишут файл).
5. `setup_cookies()` — пишет `cookies.txt` из `YT_COOKIES_B64` (атомарно).
6. `reconcile_stale_tasks()` — после рестарта помечает зависшие задачи ошибкой; `cleanup_loop()`.
7. `ERROR_PATTERNS` / `classify_error()` — сырой stderr → тип и текст для пользователя. Типы:
   `cookies_expired`, `bot`, `player`, `format`, `private`, `age_restricted`, `geo_blocked`, `not_found`,
   `copyright`, `unsupported`, `network`, `proxy_quota`, иначе `unknown`.
8. **`CLIENT_ATTEMPTS`** — список попыток `(клиент, use_cookies)`:
   `ios`, `android`, `mweb`, `web`, `tv` (все **без** cookies), затем `web` и `tv` **с** cookies.
   Cookies поставлены в конец намеренно: с IP дата-центра протухшая сессия подозрительнее анонимного запроса.
9. `QUALITY_LADDER` (2160 → 240) и `_next_lower_quality()` — понижение качества при OOM.
10. Пул прокси: `_load_proxy_pool` (`YT_PROXIES` / `YT_PROXY`), `select_proxy` (ротация), `mark_proxy_account_dead`
    (аккаунт на 402 уходит на cooldown 6 ч).
11. Выбор формата: `_best_video_format_id`, `_best_audio_format_id`, `_resolve_format_selector`
    (точный format_id из кэша, иначе общий селектор).
12. Кэш инфо и негативный кэш: `get_cached_raw_info`, `peek_cached_info`, `peek_negative_info`, `get_video_info`.
    `DEFINITIVE_ERROR_TYPES` (`private`, `age_restricted`, `geo_blocked`, `not_found`, `copyright`) не ретраятся.
13. Запуск воркера: `_run_worker_once`, `_run_worker_streaming`.
14. Перебор клиентов для инфо: `_try_one_client_for_info`, `_quality_sizes`, `_client_order`, `_fetch_video_info_uncached`
    (партии по `INFO_PARALLEL_BATCH = 3` параллельно; «бедные» данные без duration/qualities считаются неудачей клиента —
    важно для Shorts). **Выбирается не первый успешный ответ, а лучший по набору качеств** (`_info_rank`): клиенты YouTube
    отдают разные форматы (один может вернуть только `best` или 240p, другой — до 4K). Ответ с качеством ≥ `INFO_GOOD_HEIGHT = 720`
    возвращается сразу; иначе ждём остальных клиентов партии `INFO_GRACE_SECONDS = 8` с, при необходимости пробуем вторую партию
    (`INFO_MAX_QUALITY_BATCHES = 2`, не начинаем новую, если прошло > `INFO_QUALITY_SEARCH_BUDGET = 35` с). Клиент запоминается как
    «лучший глобально» только если дал ≥ 480p. Реально низкокачественные видео поэтому отвечают дольше (до ~20–35 с, один раз — потом кэш).
15. **`download_task(task_id, url, quality)`** — фоновое скачивание: старт с рабочего клиента, перебор клиентов и
    прокси, понижение качества при OOM, зависание >90 с без прогресса убивает попытку.
    **`max_attempts = len(CLIENT_ATTEMPTS) * 20`** (сейчас 140). Не уменьшать без явной просьбы владельца.
16. Прямая выдача: `_plan_stream`, `_run_worker_binary`, `_StreamSession`, `_open_stream`, `_stream_body`
    (`STREAM_*` константы).
17. Маршруты (раздел «API» выше), `/api/fetch` — `api_fetch`.

## ytdlp_worker.py

`_read_config` → `_common_opts` (клиент, `formats: missing_pot`, noplaylist, cookies, прокси, PO-token провайдер)
→ `run_info` / `run_download` (`_download_opts`, хуки прогресса и постпроцессинга) → `_emit` JSON-событий.
Режим `stream` (`run_stream`): `outtmpl = "-"`, `logtostderr`, в stdout только байты видео, ничего другого печатать нельзя.
`noplaylist = True` — именно его надо снять для будущей фичи «скачать плейлист».

## Деплой и окружение

| Переменная | Назначение |
|---|---|
| `YT_COOKIES_B64` | base64 cookies YouTube (в коде используются в последних попытках; быстро протухают) |
| `POT_PROVIDER_URL` | URL сервиса `videovault-pot-provider`; задаётся **вручную** (не `fromService`); схема `http://` добавляется, если её нет |
| `YT_PROXIES` / `YT_PROXY` | пул прокси через запятую / один прокси |
| `DEBUG_VERBOSE` | `1`/`true`/`yes` — длинные хвосты stderr в логах |
| `PORT` | задаёт Render (по умолчанию 8000) |

Подробности — в `README_DEPLOY.md`.

## Известные особенности

- Render free засыпает через ~15 минут простоя; первый запрос 15–30 с (клиент держит таймаут 90 с). RAM 512 МБ.
- yt-dlp обновляется в `start.sh` при каждом старте и **не закреплён**: ломающий релиз может положить прод при рестарте.
- Deno обязателен (расшифровка сигнатур, `yt-dlp-ejs`), без него — «Failed to extract any player response».
- Shorts на некоторых клиентах отдают пустые formats/duration: «бедный» ответ = неудача клиента.
- `--workers 1` намеренно (общий `tasks.json` и кэши в процессе); не увеличивать без проверки. `--threads 8`
  обязателен: без него (одиночный sync-воркер) один потоковый ответ блокировал бы все остальные запросы.
- Spotify скачать нельзя (DRM).

## Что смотреть при новой ошибке

1. Тип ошибки в UI → `ERROR_PATTERNS`.
2. «unsupported» / «формат недоступен» на одном видео → `_fetch_video_info_uncached`, `download_task`, PO-token сервис.
3. «cookies» / «bot» → `setup_cookies()`, `YT_COOKIES_B64`, свежесть cookies.
4. «Задача не найдена» → рестарт посреди задачи: `reconcile_stale_tasks`, логи Render.
5. Ошибка прокси (402) → аккаунт на cooldown 6 ч, проверь `YT_PROXIES`, `/health`.
6. Всегда проси логи Render: строки `[DEBUG]` содержат точный stderr yt-dlp.

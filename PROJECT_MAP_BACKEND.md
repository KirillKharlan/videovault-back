# 🗺️ КАРТА ПРОЕКТА — videovault-backend

> Этот файл — для быстрой ориентации (в том числе для Claude), чтобы не
> пересматривать все файлы при каждом фиксе. Обновляй его при структурных
> изменениях.

## Структура

```
videovault-backend/
├── app.py                 ← ВЕСЬ backend в одном файле (Flask)
├── requirements.txt        ← Python-зависимости (flask, gunicorn; yt-dlp НЕ здесь)
├── Dockerfile               ← сборка образа для Render
├── start.sh                 ← точка входа: обновляет yt-dlp, потом запускает gunicorn
├── render.yaml               ← конфиг деплоя Render (Docker runtime)
├── README_DEPLOY.md           ← инструкция по деплою и cookies
└── encoded_cookies.txt / www.youtube.com_cookies.txt
                               ← ЛОКАЛЬНЫЕ файлы для генерации YT_COOKIES_B64,
                                 на сервер НЕ заливаются (используются вручную
                                 чтобы получить base64 и вставить в Render env)
```

## app.py — карта разделов (внутри одного файла)

Порядок сверху вниз:

1. **Импорты + Flask init** — CORS открыт на все origins, request logging (`@app.before_request`)
2. **Пути**: `TMP_DIR` (видео/файлы), `TASKS_FILE` (json на диске — переживает
   рестарт/sleep сервера), `COOKIES_FILE`
3. **Хранение задач на диске** (`load_tasks`, `save_tasks`, `get_task`,
   `set_task`, `update_task`) — без кеша в памяти, всегда читают/пишут файл.
   Это специально: чтобы задачи не терялись при рестарте gunicorn.
4. **`setup_cookies()`** — пишет `cookies.txt` из env `YT_COOKIES_B64`
   (base64). Атомарная запись (tmp-файл + rename). Не перезаписывает если
   файл уже есть и не пустой.
5. **Очистка старых файлов** — фоновый поток, чистит видео/задачи старше 2ч
6. **`ERROR_PATTERNS` / `classify_error()`** — сопоставляет сырой stderr
   yt-dlp с понятным сообщением для пользователя (bot/private/format/
   unsupported и т.д.)
7. **`CLIENT_SETS`** — список наборов YouTube player-клиентов
   (`ios`, `android`, `web,mweb`, `tv_embedded`), перебираются по очереди
   при неудаче. Порядок важен: мобильные клиенты (ios/android) первые,
   т.к. лучше работают с YouTube Shorts.
8. **`ytdlp_base_args(client_set_index)`** — собирает базовые аргументы
   yt-dlp CLI с нужным набором клиентов + cookies
9. **Кеш инфо о видео** (`_info_cache`, TTL 10 мин) — ключ: URL,
   значение: `(timestamp, info_dict, working_client_index)`.
   Важно: сохраняет **какой именно клиент сработал**, чтобы скачивание
   потом начиналось с того же клиента, а не с дефолтного.
10. **`get_video_info(url)`** — публичная точка входа, проверяет кеш,
    иначе зовёт `_fetch_video_info_uncached`
11. **`_fetch_video_info_uncached(url, client_index, attempts_left)`** —
    рекурсивно перебирает `CLIENT_SETS` пока не получит вменяемые данные
    (проверяет что duration>0 или есть качества — иначе тоже считает
    неудачей и пробует следующего клиента)
12. **`download_task(task_id, url, quality)`** — фоновая задача:
    - берёт `working_client_index` из кеша инфо (тот же клиент что сработал)
    - цикл `for attempt in range(max_attempts)` — перебирает клиентов начиная
      с рабочего, если скачивание вдруг не удаётся
    - парсит прогресс из stdout yt-dlp построчно, обновляет `update_task`
    - при успехе — `status=done`, при неудаче после всех попыток — `status=error`
13. **API роуты**:
    - `GET /health` — версия yt-dlp, наличие cookies, кол-во задач
    - `POST /api/info` — вызывает `get_video_info`, отдаёт JSON (без
      служебного поля `_client_index`)
    - `POST /api/download` — создаёт task_id, запускает `download_task` в
      отдельном потоке, сразу возвращает task_id
    - `GET /api/progress/<task_id>` — статус задачи
    - `GET /api/file/<task_id>` — отдаёт готовый файл

## Известные особенности / грабли

- **Render free plan засыпает** через 15 мин простоя — первый запрос будит
  сервер ~15-30 сек. Flutter должен иметь таймауты 90+ сек на первый запрос.
- **yt-dlp обновляется в `start.sh`**, НЕ в requirements.txt — так он всегда
  последней версии при каждом деплое/рестарте.
- **Задачи хранятся в файле**, не в памяти — переживают рестарт процесса.
- **YouTube Shorts** могут отдавать пустые formats/duration с некоторыми
  клиентами (tv_embedded/web_creator) — поэтому mobile-клиенты идут первыми
  в `CLIENT_SETS`, и есть проверка "poor data" которая триггерит fallback.
- **Cookies обязательны** для стабильной работы — без них YouTube помечает
  запросы с IP дата-центра Render как ботов.

## Что смотреть при новой ошибке

1. Сначала — тип ошибки в UI (текст из `ERROR_PATTERNS`)
2. Если "unsupported"/"формат недоступен" на конкретном видео — скорее
   всего проблема в `CLIENT_SETS` или в fallback-логике `download_task`
3. Если "cookies"/"bot" — смотри `setup_cookies()` и переменную
   `YT_COOKIES_B64` на Render
4. Если "задача не найдена" — смотри `load_tasks`/`save_tasks`, возможно
   сервер перезапустился в процессе (см. логи Render на предмет рестарта)
5. Логи Render показывают `[DEBUG]` строки с точным stderr от yt-dlp —
   всегда проси их у пользователя при новой ошибке, это экономит время

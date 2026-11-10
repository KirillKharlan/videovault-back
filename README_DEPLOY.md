# Деплой на Render.com

Бэкенд работает в **Docker** (`Dockerfile` + `start.sh` + gunicorn `app:app`). Из двух сервисов в
`render.yaml` нужен и сервер, и PO-token провайдер.

## Что разворачивается

| Сервис | Тип | Что это |
|---|---|---|
| `videovault-server` | Web Service, runtime `docker`, plan Free | Flask-бэкенд из этого репозитория |
| `videovault-pot-provider` | Web Service, runtime `image` | `docker.io/brainicism/bgutil-ytdlp-pot-provider:latest`, выдаёт PO Token |

## Шаги

1. Залей папку `videovault-backend` на GitHub (cookies-файлы **не коммить**, см. ниже).
2. Render → New → Blueprint (или два Web Service вручную) → подключи репозиторий; `render.yaml` создаст оба сервиса.
3. Дождись деплоя `videovault-pot-provider` и скопируй его публичный URL, например
   `https://videovault-pot-provider.onrender.com`.
4. В `videovault-server` → Environment задай переменные:

   | Переменная | Значение |
   |---|---|
   | `POT_PROVIDER_URL` | URL из шага 3. Задаётся вручную: `fromService` ломается, если сервисы в разных Projects |
   | `YT_COOKIES_B64` | base64 от `cookies.txt` (необязательно, см. ниже) |
   | `YT_PROXIES` | прокси через запятую, формат `http://user:pass@host:port` (необязательно) |
   | `DEBUG_VERBOSE` | `1` для подробных логов (необязательно) |

5. Скопируй URL `videovault-server` и впиши его в `videovault-app/lib/api/api_client.dart` (`baseUrl`).

Менять Build/Start command не нужно: всё задано в `Dockerfile` (`CMD ["./start.sh"]`).

## Что происходит при старте (`start.sh`)

1. Печатает версии yt-dlp, ffmpeg и Deno.
2. `pip install --upgrade yt-dlp yt-dlp-ejs` — yt-dlp обновляется при **каждом** запуске и не закреплён.
3. `pip install --upgrade bgutil-ytdlp-pot-provider` — плагин для PO Token.
4. Запускает `gunicorn app:app --workers 1 --threads 8 --timeout 300` на `$PORT` (по умолчанию 8000).
   Потоки нужны, чтобы во время потоковой выдачи видео (`/api/fetch`) сервер отвечал на остальные запросы.

Зависимости Flask, flask-cors и gunicorn ставит Dockerfile (flask 3.1.0, flask-cors 5.0.0, gunicorn 23.0.0).
`requirements.txt` в Docker-сборке не используется, не полагайся на него.

## Cookies YouTube

- Получить base64 (Windows PowerShell):
  `[Convert]::ToBase64String([IO.File]::ReadAllBytes("cookies.txt")) | Set-Clipboard`
  и вставить в `YT_COOKIES_B64`.
- Cookies быстро протухают, когда используются с серверного IP (`cookies are no longer valid`,
  «Sign in to confirm you're not a bot»). В коде сначала пробуются анонимные клиенты, cookies стоят в конце списка.
- Файлы `cookies.txt`, `www.youtube.com_cookies.txt`, `encoded_cookies.txt` — учётные данные.
  Не коммить и не пересылай их. `.gitignore` сейчас содержит только первые два; `encoded_cookies.txt` нужно добавить.

## Проверка после деплоя

Открой `https://<твой-url>.onrender.com/health`. Ответ похож на:

```json
{"ok": true, "yt_dlp_version": "…", "deno_version": "deno …", "cookies": true,
 "debug_verbose": false, "proxy_count": 0, "proxy_rotation_position": 0,
 "proxy_accounts_on_cooldown": {}, "tasks": 0}
```

Если `deno_version` содержит `NOT FOUND` или `ERROR`, YouTube-извлечение работать не будет.

## Free-план Render

- Сервис засыпает через ~15 минут простоя, первый запрос после сна занимает 15–40 секунд.
- 512 МБ RAM: тяжёлые видео (4K/1440p) могут убить процесс по памяти; бэкенд в этом случае сам понижает качество.
- Диск эфемерный: файлы и `tasks.json` пропадают при передеплое; задачи, прерванные рестартом, помечаются ошибкой.

# Деплой на Render.com

## Вариант A — из PyPI (проще)
requirements.txt уже содержит `yt-dlp==2026.7.4`.
Просто деплой репо как есть.

## Вариант B — из исходников (если PyPI отстаёт)
Если на PyPI ещё нет нужной версии, замени строку в requirements.txt:

```
# вместо: yt-dlp==2026.7.4
yt-dlp @ https://github.com/yt-dlp/yt-dlp/archive/refs/heads/master.zip
```

Или установи конкретный коммит:
```
yt-dlp @ git+https://github.com/yt-dlp/yt-dlp.git@997fa140840a08df3938b40da470c78049fef1f6
```
(это хэш коммита версии 2026.07.04 из zip файла)

## Шаги деплоя

1. Создай репо на GitHub, залей папку videovault-backend
2. Render.com → New → Web Service → подключи репо
3. Настройки:
   - **Runtime**: Python 3.11
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Plan**: Free
4. Deploy → скопируй URL вида `https://xxx.onrender.com`
5. Вставь URL в `videovault-app/lib/api/api_client.dart`

## Проверка после деплоя

Открой в браузере: `https://твой-url.onrender.com/health`

Должно вернуть:
```json
{"status": "ok", "yt_dlp_version": "2026.07.04"}
```

## Важно про Free план Render

Бесплатный сервер "засыпает" после 15 минут неактивности.
Первый запрос после сна займёт ~30 секунд — это нормально.

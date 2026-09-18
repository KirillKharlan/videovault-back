#!/bin/bash
set -e

echo "=== VideoVault Backend Starting ==="
echo "[startup] yt-dlp version at start: $(yt-dlp --version 2>/dev/null || echo 'not installed')"
echo "[startup] ffmpeg: $(ffmpeg -version 2>&1 | head -1)"
echo "[startup] deno: $(deno --version 2>&1 | head -1 || echo 'NOT FOUND — signature extraction will fail!')"

# Обновляем yt-dlp ДО старта gunicorn — один раз при деплое
echo "[startup] Updating yt-dlp to latest..."
pip install --quiet --upgrade yt-dlp yt-dlp-ejs
echo "[startup] yt-dlp updated to: $(yt-dlp --version)"

# PO Token provider (bgutil) — Python-плагин на стороне yt-dlp, который
# обращается к отдельному Node.js-сервису (см. POT_PROVIDER_URL ниже) за
# самими токенами. Без него yt-dlp просто не подключит поддержку PO Token
# вообще, даже если POT_PROVIDER_URL задан.
echo "[startup] Installing bgutil-ytdlp-pot-provider plugin..."
pip install --quiet --upgrade bgutil-ytdlp-pot-provider

echo "[startup] Starting gunicorn..."
exec gunicorn app:app \
  --workers 1 \
  --timeout 300 \
  --bind "0.0.0.0:${PORT:-8000}" \
  --log-level info \
  --access-logfile - \
  --error-logfile -

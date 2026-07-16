#!/bin/bash
set -e

echo "=== VideoVault Backend Starting ==="
echo "[startup] yt-dlp version at start: $(yt-dlp --version 2>/dev/null || echo 'not installed')"
echo "[startup] ffmpeg: $(ffmpeg -version 2>&1 | head -1)"

# Обновляем yt-dlp ДО старта gunicorn — один раз при деплое
# После этого gunicorn запускается и больше не перезапускается
echo "[startup] Updating yt-dlp to latest..."
pip install --quiet --upgrade yt-dlp yt-dlp-ejs
echo "[startup] yt-dlp updated to: $(yt-dlp --version)"

echo "[startup] Starting gunicorn..."
exec gunicorn app:app \
  --workers 1 \
  --timeout 300 \
  --bind "0.0.0.0:${PORT:-8000}" \
  --log-level info \
  --access-logfile - \
  --error-logfile -

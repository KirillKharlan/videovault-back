#!/bin/bash
set -e

echo "=== VideoVault Backend Starting ==="

# Устанавливаем/обновляем yt-dlp до последней версии при каждом запуске
# Это важно — YouTube меняет защиту каждые 1-2 недели
echo "[startup] Installing latest yt-dlp..."
pip install --quiet --upgrade yt-dlp yt-dlp-ejs

echo "[startup] yt-dlp version: $(yt-dlp --version)"
echo "[startup] ffmpeg version: $(ffmpeg -version 2>&1 | head -1)"

# Запускаем gunicorn
echo "[startup] Starting gunicorn..."
exec gunicorn app:app \
  --workers 2 \
  --timeout 300 \
  --bind "0.0.0.0:${PORT:-8000}" \
  --log-level info \
  --access-logfile - \
  --error-logfile -

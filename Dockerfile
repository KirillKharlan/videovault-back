FROM python:3.11-slim

# ffmpeg нужен для склейки аудио+видео
RUN apt-get update && apt-get install -y ffmpeg curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Устанавливаем всё кроме yt-dlp (эти зависимости не меняются)
COPY requirements.txt .
RUN pip install --no-cache-dir flask==3.1.0 flask-cors==5.0.0 gunicorn==23.0.0 \
    brotli pycryptodomex mutagen requests urllib3 websockets certifi

COPY app.py .
COPY start.sh .
RUN chmod +x start.sh

EXPOSE 8000
# Запускаем через start.sh который сначала обновляет yt-dlp
CMD ["./start.sh"]

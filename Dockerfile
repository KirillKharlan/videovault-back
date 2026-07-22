FROM python:3.11-slim

# ffmpeg — склейка аудио+видео
# curl — установка Deno
RUN apt-get update && apt-get install -y ffmpeg curl unzip && rm -rf /var/lib/apt/lists/*

# Deno — JS-движок, ОБЯЗАТЕЛЕН для yt-dlp >= 2026.x
# Без него YouTube-экстрактор не может расшифровать сигнатуры видео и
# падает с ошибкой "Failed to extract any player response" на ЛЮБОМ клиенте.
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y \
    && mv /root/.deno/bin/deno /usr/local/bin/deno \
    && chmod +x /usr/local/bin/deno \
    && deno --version

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

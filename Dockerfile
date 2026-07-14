FROM python:3.11-slim

# Install ffmpeg (needed by yt-dlp)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD gunicorn app:app --workers 2 --timeout 300 --bind 0.0.0.0:${PORT:-8000}

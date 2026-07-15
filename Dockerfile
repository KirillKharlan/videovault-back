FROM python:3.11-slim

# Встановлюємо ffmpeg (необхідний для yt-dlp)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копіюємо та встановлюємо залежності
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копіюємо весь код додатка
COPY . .

EXPOSE 8000

# Запускаємо через gunicorn, використовуючи асинхронний клас uvicorn.workers.UvicornWorker
# (переконайтеся, що в requirements.txt є uvicorn, gunicorn)
CMD gunicorn main:app --workers 2 --timeout 300 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:${PORT:-8000}
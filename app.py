"""
VideoVault Backend — единый файл
Маршруты:
  GET  /health
  POST /api/info          — получить инфо о видео
  POST /api/download      — запустить скачивание, вернуть task_id
  GET  /api/progress/<id> — прогресс задачи
  GET  /api/file/<id>     — скачать готовый файл
"""

import os, json, threading, subprocess, time, uuid, tempfile, re
from pathlib import Path
from flask import Flask, request, jsonify, send_file, abort
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins="*")

TMP_DIR = Path(tempfile.gettempdir()) / "videovault"
TMP_DIR.mkdir(exist_ok=True)

# task_id -> { status, percent, step, title, file_path, error, error_type, created_at }
tasks = {}


# ── Очистка старых файлов ─────────────────────────────────────────────────────

def cleanup_loop():
    while True:
        time.sleep(3600)
        now = time.time()
        for tid, t in list(tasks.items()):
            if now - t.get('created_at', now) > 7200:  # 2 часа
                fp = t.get('file_path')
                if fp:
                    Path(fp).unlink(missing_ok=True)
                tasks.pop(tid, None)

threading.Thread(target=cleanup_loop, daemon=True).start()


# ── Классификатор ошибок ──────────────────────────────────────────────────────

ERROR_PATTERNS = [
    (['private video', 'video is private'],
     'private', '🔒 Видео приватное'),
    (['age-restricted', 'sign in to confirm your age'],
     'age_restricted', '🔞 Видео с ограничением по возрасту'),
    (['not available in your country', 'blocked in your country'],
     'geo_blocked', '🌍 Видео недоступно в регионе сервера'),
    (['video unavailable', 'has been removed', 'does not exist'],
     'not_found', '🔍 Видео не найдено или удалено'),
    (['copyright', 'removed by'],
     'copyright', '©️ Видео удалено за нарушение авторских прав'),
    (['unsupported url', 'no video formats', 'unable to extract'],
     'unsupported', '❌ Эта ссылка не поддерживается'),
    (['urlopen error', 'connection', 'timed out'],
     'network', '📡 Ошибка сети'),
]

def classify_error(text: str) -> tuple[str, str]:
    low = text.lower()
    for patterns, err_type, message in ERROR_PATTERNS:
        if any(p in low for p in patterns):
            return err_type, message
    return 'unknown', f'⚠️ Ошибка: {text[:200]}'


# ── yt-dlp базовые аргументы ──────────────────────────────────────────────────

def ytdlp_base_args() -> list[str]:
    """
    Аргументы для yt-dlp 2026.07.04.
    iOS клиент обходит bot-detection YouTube без po_token.
    """
    return [
        'yt-dlp',
        '--no-playlist',
        '--no-warnings',
        '--extractor-args', 'youtube:player_client=ios,web',
        '--add-header', 'User-Agent:com.google.ios.youtube/19.45.4 (iPhone16,2; U; CPU iOS 18_1_0 like Mac OS X)',
    ]


# ── Получение информации о видео ──────────────────────────────────────────────

def get_video_info(url: str) -> dict:
    try:
        cmd = ytdlp_base_args() + ['--dump-json', url]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)

        if r.returncode == 0 and r.stdout.strip():
            info = json.loads(r.stdout.strip().split('\n')[0])

            # Собираем доступные качества
            qualities = set()
            for f in info.get('formats', []):
                h = f.get('height')
                vcodec = f.get('vcodec', 'none')
                if h and h >= 240 and vcodec != 'none':
                    qualities.add(h)

            sorted_q = sorted(qualities, reverse=True)
            quality_labels = [str(q) for q in sorted_q] or ['best']

            return {
                'title':     info.get('title', 'Видео'),
                'thumbnail': info.get('thumbnail', ''),
                'duration':  int(info.get('duration') or 0),
                'uploader':  info.get('uploader', ''),
                'platform':  info.get('extractor_key', '').lower(),
                'qualities': quality_labels,
            }

        err_type, message = classify_error(r.stderr + r.stdout)
        return {'error': message, 'error_type': err_type}

    except subprocess.TimeoutExpired:
        return {'error': '⏱ Таймаут — сайт не ответил', 'error_type': 'network'}
    except Exception as e:
        return {'error': str(e), 'error_type': 'unknown'}


# ── Задача скачивания ─────────────────────────────────────────────────────────

def download_task(task_id: str, url: str, quality: str):
    def upd(**kw):
        tasks[task_id].update(kw)

    upd(status='fetching_info', percent=0, step='Получение информации…', created_at=time.time())

    # Шаг 1: мета
    info = get_video_info(url)
    if 'error' in info:
        upd(status='error', error=info['error'], error_type=info.get('error_type', 'unknown'))
        return

    title = info['title']
    upd(title=title, step='Подготовка…')

    # Шаг 2: формат
    if quality.isdigit():
        fmt = (f'bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]'
               f'/best[height<={quality}][ext=mp4]/best[height<={quality}]/best')
    else:
        fmt = 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]/best'

    safe = re.sub(r'[^\w\sа-яА-Я.-]', '', title)[:60].strip() or 'video'
    out = str(TMP_DIR / f"{task_id}_{safe}.%(ext)s")

    cmd = ytdlp_base_args() + [
        '--newline',
        '-f', fmt,
        '--merge-output-format', 'mp4',
        '-o', out,
        url,
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout_lines = []

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        stdout_lines.append(line)

        if '[download]' in line and '%' in line:
            m_pct   = re.search(r'(\d+\.?\d*)%', line)
            m_speed = re.search(r'at\s+([\d.]+\s*\w+/s)', line)
            m_eta   = re.search(r'ETA\s+([\d:]+)', line)
            m_size  = re.search(r'of\s+([\d.]+\s*\w+)', line)

            if m_pct:
                upd(percent=min(95, float(m_pct.group(1))), status='downloading')

            parts = []
            if m_size:  parts.append(f'Размер: {m_size.group(1)}')
            if m_speed: parts.append(f'Скорость: {m_speed.group(1)}')
            if m_eta:   parts.append(f'Осталось: {m_eta.group(1)}')
            if parts:   upd(step=' · '.join(parts))

        elif '[Merger]' in line or 'Merging' in line:
            upd(step='🔀 Объединение аудио и видео…', percent=97)
        elif 'Destination:' in line:
            upd(step='💾 Сохранение…')

    stderr_out = proc.stderr.read()
    proc.wait()

    if proc.returncode != 0:
        err_type, message = classify_error(stderr_out + '\n'.join(stdout_lines))
        upd(status='error', error=message, error_type=err_type,
            error_detail=stderr_out[-500:])
        return

    files = list(TMP_DIR.glob(f"{task_id}_*"))
    if not files:
        upd(status='error', error='⚠️ Файл не создан', error_type='unknown')
        return

    vp = max(files, key=lambda f: f.stat().st_size)
    upd(status='done', percent=100, step='✓ Готово!',
        file_path=str(vp), filename=vp.name, file_size=vp.stat().st_size)


# ── API маршруты ──────────────────────────────────────────────────────────────

@app.get('/health')
def health():
    # Проверяем что yt-dlp вообще есть
    try:
        r = subprocess.run(['yt-dlp', '--version'], capture_output=True, text=True, timeout=5)
        ytdlp_ver = r.stdout.strip()
    except Exception:
        ytdlp_ver = 'not found'
    return jsonify({'ok': True, 'yt_dlp_version': ytdlp_ver, 'tasks': len(tasks)})


@app.post('/api/info')
def api_info():
    url = (request.json or {}).get('url', '').strip()
    if not url:
        return jsonify({'error': '❌ URL не указан', 'error_type': 'validation'}), 400
    result = get_video_info(url)
    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result)


@app.post('/api/download')
def api_download():
    body = request.json or {}
    url = body.get('url', '').strip()
    quality = body.get('quality', 'best').strip()
    if not url:
        return jsonify({'error': '❌ URL не указан', 'error_type': 'validation'}), 400
    task_id = uuid.uuid4().hex[:12]
    tasks[task_id] = {'status': 'queued', 'percent': 0, 'created_at': time.time()}
    threading.Thread(target=download_task, args=(task_id, url, quality), daemon=True).start()
    return jsonify({'task_id': task_id})


@app.get('/api/progress/<task_id>')
def api_progress(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Задача не найдена', 'error_type': 'not_found'}), 404
    return jsonify({k: v for k, v in task.items() if k != 'file_path'})


@app.get('/api/file/<task_id>')
def api_file(task_id):
    task = tasks.get(task_id)
    if not task or task.get('status') != 'done':
        abort(404)
    fp = task.get('file_path')
    if not fp or not Path(fp).exists():
        abort(404)
    return send_file(fp, mimetype='video/mp4', as_attachment=True,
                     download_name=task.get('filename', 'video.mp4'))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)))

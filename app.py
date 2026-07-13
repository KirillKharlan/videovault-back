"""
VideoVault Cloud Server
- Детальный прогресс с шагами (скорость, ETA, размер)
- Классификация ошибок с понятными сообщениями
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


def cleanup_loop():
    while True:
        time.sleep(7200)
        now = time.time()
        for tid, t in list(tasks.items()):
            if now - t.get('created_at', now) > 7200:
                fp = t.get('file_path')
                if fp and Path(fp).exists():
                    Path(fp).unlink(missing_ok=True)
                tasks.pop(tid, None)

threading.Thread(target=cleanup_loop, daemon=True).start()


# ── Классификатор ошибок ─────────────────────────────────────────────────────

ERROR_PATTERNS = [
    (['private video', 'video is private'],
     'private', '🔒 Видео приватное — автор закрыл доступ'),

    (['age-restricted', 'sign in to confirm your age', 'age restricted'],
     'age_restricted', '🔞 Видео с ограничением по возрасту'),

    (['not available in your country', 'blocked in your country', 'geo'],
     'geo_blocked', '🌍 Видео недоступно в регионе сервера'),

    (['video unavailable', 'has been removed', 'does not exist', '404'],
     'not_found', '🔍 Видео не найдено или удалено'),

    (['copyright', 'removed by'],
     'copyright', '©️ Видео удалено за нарушение авторских прав'),

    (['unsupported url', 'no video formats', 'unable to extract'],
     'unsupported', '❌ Эта ссылка не поддерживается'),

    (['urlopen error', 'connection', 'timed out', 'network error'],
     'network', '📡 Ошибка сети — сервер не смог подключиться к сайту'),
]


def classify_error(text: str) -> tuple[str, str]:
    low = text.lower()
    for patterns, err_type, message in ERROR_PATTERNS:
        if any(p in low for p in patterns):
            return err_type, message
    return 'unknown', '⚠️ Неизвестная ошибка — попробуй другую ссылку'


# ── Получение информации о видео ─────────────────────────────────────────────

def get_video_info(url: str) -> dict:
    try:
        r = subprocess.run(
            ['yt-dlp', '--dump-json', '--no-playlist', '--no-warnings', url],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            info = json.loads(r.stdout.strip().split('\n')[0])
            return {
                'title':           info.get('title', 'Видео'),
                'thumbnail':       info.get('thumbnail', ''),
                'duration':        int(info.get('duration') or 0),
                'uploader':        info.get('uploader', ''),
                'platform':        info.get('extractor_key', '').lower(),
                'filesize_approx': info.get('filesize_approx', 0),
            }
        err_type, message = classify_error(r.stderr + r.stdout)
        return {'error': message, 'error_type': err_type}
    except subprocess.TimeoutExpired:
        return {'error': '⏱ Таймаут — сайт не ответил за 30 сек', 'error_type': 'network'}
    except Exception as e:
        return {'error': str(e), 'error_type': 'unknown'}


# ── Задача скачивания ─────────────────────────────────────────────────────────

def download_task(task_id: str, url: str):
    def upd(**kw):
        tasks[task_id].update(kw)

    tasks[task_id].update(status='fetching_info', percent=0,
                           step='Получение информации о видео...', created_at=time.time())

    # Шаг 1: получаем мета
    info = get_video_info(url)
    if 'error' in info:
        upd(status='error', error=info['error'], error_type=info['error_type'],
            step='Ошибка получения информации')
        return

    title = info['title']
    upd(status='downloading', title=title, step='Подготовка к скачиванию...')

    # Шаг 2: скачиваем
    safe = re.sub(r'[^\w\sа-яА-Я.-]', '', title)[:60].strip() or 'video'
    out = str(TMP_DIR / f"{task_id}_{safe}.%(ext)s")

    cmd = [
        'yt-dlp', '--no-playlist', '--newline', '--no-warnings',
        '-f', 'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]/best',
        '--merge-output-format', 'mp4',
        '-o', out, url,
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout_buf, stderr_buf = [], []

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        stdout_buf.append(line)

        # Прогресс: "[download] 45.3% of 123.4MiB at 2.50MiB/s ETA 00:30"
        if '[download]' in line and '%' in line:
            m_pct   = re.search(r'(\d+\.?\d*)%', line)
            m_size  = re.search(r'of\s+([\d.]+\s*\w+)', line)
            m_speed = re.search(r'at\s+([\d.]+\s*\w+/s)', line)
            m_eta   = re.search(r'ETA\s+([\d:]+)', line)

            if m_pct:
                upd(percent=min(99, float(m_pct.group(1))))

            # Строим понятный текст шага
            parts = []
            if m_size:  parts.append(f'Размер: {m_size.group(1)}')
            if m_speed: parts.append(f'Скорость: {m_speed.group(1)}')
            if m_eta:   parts.append(f'Осталось: {m_eta.group(1)}')
            if parts:   upd(step=' · '.join(parts))

        elif '[Merger]' in line or 'Merging' in line:
            upd(step='🔀 Объединение аудио и видео...')
        elif 'Destination:' in line:
            upd(step='💾 Сохранение файла...')

    stderr_buf = proc.stderr.read()
    proc.wait()

    if proc.returncode != 0:
        err_type, message = classify_error(stderr_buf + '\n'.join(stdout_buf))
        # Берём последние строки stderr для детального лога
        detail = '\n'.join(
            l for l in stderr_buf.splitlines()[-8:] if l.strip()
        )
        upd(status='error', error=message, error_type=err_type,
            error_detail=detail, step='Ошибка скачивания')
        return

    # Шаг 3: ищем файл
    files = list(TMP_DIR.glob(f"{task_id}_*"))
    if not files:
        upd(status='error', error='⚠️ Файл не создан — видео могло быть защищено',
            error_type='unknown')
        return

    vp = files[0]
    upd(status='done', percent=100, step='✓ Готово!',
        file_path=str(vp), filename=vp.name, file_size=vp.stat().st_size)


# ── API ───────────────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify({'ok': True, 'tasks': len(tasks)})


@app.route('/api/info', methods=['POST'])
def api_info():
    url = (request.json or {}).get('url', '').strip()
    if not url:
        return jsonify({'error': '❌ URL не указан', 'error_type': 'validation'}), 400
    result = get_video_info(url)
    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result)


@app.route('/api/download', methods=['POST'])
def api_download():
    url = (request.json or {}).get('url', '').strip()
    if not url:
        return jsonify({'error': '❌ URL не указан', 'error_type': 'validation'}), 400
    task_id = uuid.uuid4().hex[:12]
    tasks[task_id] = {'status': 'queued', 'percent': 0, 'created_at': time.time()}
    threading.Thread(target=download_task, args=(task_id, url), daemon=True).start()
    return jsonify({'task_id': task_id})


@app.route('/api/progress/<task_id>')
def api_progress(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Задача не найдена', 'error_type': 'not_found'}), 404
    # Не отдаём внутренний путь к файлу
    return jsonify({k: v for k, v in task.items() if k != 'file_path'})


@app.route('/api/file/<task_id>')
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

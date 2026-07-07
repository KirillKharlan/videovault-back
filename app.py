"""
VideoVault Cloud Server
Деплоится на Render.com — скачивает видео через yt-dlp
"""

import os, json, threading, subprocess, time, uuid, tempfile, shutil
from pathlib import Path
from flask import Flask, request, jsonify, send_file, abort
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins="*")

# Временная папка для видео (Render не имеет постоянного диска на free tier)
TMP_DIR = Path(tempfile.gettempdir()) / "videovault"
TMP_DIR.mkdir(exist_ok=True)

# Хранилище задач в памяти
tasks = {}  # task_id -> {status, percent, title, file_path, error, created_at}

# Чистим старые файлы каждые 2 часа
def cleanup_old_files():
    while True:
        time.sleep(7200)
        now = time.time()
        for task_id, task in list(tasks.items()):
            if now - task.get('created_at', now) > 7200:
                fp = task.get('file_path')
                if fp and Path(fp).exists():
                    Path(fp).unlink(missing_ok=True)
                tasks.pop(task_id, None)

threading.Thread(target=cleanup_old_files, daemon=True).start()


def get_video_info(url):
    try:
        result = subprocess.run(
            ['yt-dlp', '--dump-json', '--no-playlist', '--no-warnings', url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            info = json.loads(result.stdout.strip().split('\n')[0])
            return {
                'title': info.get('title', 'Видео'),
                'thumbnail': info.get('thumbnail', ''),
                'duration': int(info.get('duration') or 0),
                'uploader': info.get('uploader', ''),
                'platform': info.get('extractor_key', 'Unknown').lower(),
                'filesize_approx': info.get('filesize_approx', 0),
            }
    except Exception as e:
        print(f"Info error: {e}")
    return None


def download_task(task_id, url):
    tasks[task_id].update({'status': 'fetching_info', 'percent': 0, 'created_at': time.time()})

    try:
        info = get_video_info(url)
        title = info['title'] if info else 'video'
        tasks[task_id]['title'] = title
        tasks[task_id]['info'] = info

        tasks[task_id]['status'] = 'downloading'

        safe = "".join(c for c in title if c.isalnum() or c in ' -_')[:60].strip() or 'video'
        out_template = str(TMP_DIR / f"{task_id}_{safe}.%(ext)s")

        cmd = [
            'yt-dlp',
            '--no-playlist',
            '-f', 'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]/best[height<=720]/best',
            '--merge-output-format', 'mp4',
            '--no-warnings',
            '--newline',
            '-o', out_template,
            url
        ]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        for line in proc.stdout:
            line = line.strip()
            if '[download]' in line and '%' in line:
                try:
                    for part in line.split():
                        if '%' in part:
                            tasks[task_id]['percent'] = min(99, float(part.replace('%', '')))
                            break
                except:
                    pass

        proc.wait()

        if proc.returncode != 0:
            raise Exception("yt-dlp завершился с ошибкой. Возможно ссылка приватная или недоступна.")

        files = list(TMP_DIR.glob(f"{task_id}_*"))
        if not files:
            raise Exception("Файл не создан после загрузки")

        video_path = str(files[0])
        file_size = Path(video_path).stat().st_size

        tasks[task_id].update({
            'status': 'done',
            'percent': 100,
            'file_path': video_path,
            'filename': Path(video_path).name,
            'file_size': file_size,
        })

    except Exception as e:
        tasks[task_id].update({'status': 'error', 'error': str(e)})


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify({'ok': True, 'tasks': len(tasks)})


@app.route('/api/info', methods=['POST'])
def api_info():
    url = (request.json or {}).get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL не указан'}), 400
    info = get_video_info(url)
    if info:
        return jsonify(info)
    return jsonify({'error': 'Не удалось получить информацию. Проверь ссылку.'}), 400


@app.route('/api/download', methods=['POST'])
def api_download():
    url = (request.json or {}).get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL не указан'}), 400

    task_id = uuid.uuid4().hex[:12]
    tasks[task_id] = {'status': 'queued', 'percent': 0, 'title': '', 'created_at': time.time()}

    threading.Thread(target=download_task, args=(task_id, url), daemon=True).start()
    return jsonify({'task_id': task_id})


@app.route('/api/progress/<task_id>')
def api_progress(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Задача не найдена'}), 404
    # Не отдаём file_path клиенту
    safe = {k: v for k, v in task.items() if k not in ('file_path',)}
    return jsonify(safe)


@app.route('/api/file/<task_id>')
def api_file(task_id):
    task = tasks.get(task_id)
    if not task or task.get('status') != 'done':
        abort(404)
    fp = task.get('file_path')
    if not fp or not Path(fp).exists():
        abort(404)
    return send_file(
        fp,
        mimetype='video/mp4',
        as_attachment=True,
        download_name=task.get('filename', 'video.mp4')
    )


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)

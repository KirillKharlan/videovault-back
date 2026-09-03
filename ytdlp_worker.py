"""
ytdlp_worker.py — использует yt-dlp как Python-библиотеку (а не CLI-бинарь),
но запускается родительским процессом (app.py) как ОТДЕЛЬНЫЙ ПОДПРОЦЕСС —
намеренно, а не напрямую внутри Flask/gunicorn:

  - Если это видео убьёт процесс по памяти (OOM), погибнет только этот
    подпроцесс — как и раньше с CLI-вызовами yt-dlp. Родительский
    gunicorn-воркер и все параллельные задачи других пользователей не
    пострадают. Прямой импорт yt_dlp внутри Flask убрал бы эту изоляцию.
  - Родитель по-прежнему видит returncode < 0 при убийстве сигналом — вся
    существующая логика понижения качества при OOM (см. app.py) работает
    без изменений.

Протокол: конфиг приходит в stdin одной JSON-строкой, результат построчно
пишется в stdout как JSON (по одной строке на событие). Ошибки — обычные
исключения, текст уходит в stderr (в него же попадает текст самого
исключения yt-dlp — то же самое, что раньше парсил classify_error() из
stderr CLI-вызова, так что вся классификация ошибок продолжает работать).

Режимы (config["mode"]):
  "info"     — извлекает метаданные БЕЗ скачивания. Пишет одну строку
               {"type": "info", "info": {...}} с "сырым" (sanitized) info
               dict — его можно закешировать и передать сюда же в режиме
               "download" как cached_info, чтобы не ходить к YouTube второй
               раз за тем же видео.
  "download" — качает видео/аудио. Если передан cached_info и он ещё не
               протух (не истекли подписанные ссылки на файл) — скачивание
               идёт БЕЗ повторного извлечения. Если cached_info не сработал
               (например, ссылки протухли) — автоматически откатывается на
               обычное извлечение+скачивание за один проход, как раньше.
"""
import sys
import json
import yt_dlp


def _read_config() -> dict:
    raw = sys.stdin.read()
    return json.loads(raw)


def _common_opts(cfg: dict) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "extractor_args": {"youtube": {"player_client": [cfg["client"]]}},
    }
    if cfg.get("verbose"):
        opts["verbose"] = True
    if cfg.get("use_cookies") and cfg.get("cookies_path"):
        opts["cookiefile"] = cfg["cookies_path"]
    if cfg.get("proxy"):
        opts["proxy"] = cfg["proxy"]
    return opts


def _emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run_info(cfg: dict) -> None:
    opts = _common_opts(cfg)
    opts["ignore_no_formats_error"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(cfg["url"], download=False)
        info = ydl.sanitize_info(info)
    _emit({"type": "info", "info": info})


def _format_eta(seconds) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _format_bytes(n) -> str:
    if not n:
        return ""
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TiB"


def _make_progress_hook():
    def hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100) if total else None
            _emit({
                "type": "progress",
                "percent": round(percent, 1) if percent is not None else None,
                "speed": _format_bytes(d.get("speed")) + "/s" if d.get("speed") else "",
                "eta": _format_eta(d.get("eta")),
                "total": _format_bytes(total),
            })
        elif d.get("status") == "finished":
            _emit({"type": "step", "text": "💾 Сохранение…"})
    return hook


def _make_postprocessor_hook():
    def hook(d: dict) -> None:
        if d.get("status") == "started":
            name = d.get("postprocessor", "")
            if "Merger" in name:
                _emit({"type": "step", "text": "🔀 Объединение аудио и видео…"})
            elif "ExtractAudio" in name:
                _emit({"type": "step", "text": "🎵 Извлечение аудио в MP3…"})
    return hook


def _download_opts(cfg: dict) -> dict:
    opts = _common_opts(cfg)
    opts["outtmpl"] = cfg["output_template"]
    opts["ignore_no_formats_error"] = True
    opts["postprocessor_args"] = {"ffmpeg": ["-threads", "1"]}
    opts["progress_hooks"] = [_make_progress_hook()]
    opts["postprocessor_hooks"] = [_make_postprocessor_hook()]

    if cfg.get("is_audio_only"):
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "0",
        }]
    else:
        h = cfg.get("height")
        if h:
            opts["format"] = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
            opts["format_sort"] = [f"res:{h}", "ext:mp4:m4a", "+codec:avc:m4a"]
        else:
            opts["format"] = "bestvideo+bestaudio/best"
            opts["format_sort"] = ["res", "ext:mp4:m4a", "+codec:avc:m4a"]
        opts["merge_output_format"] = "mp4"
    return opts


def run_download(cfg: dict) -> None:
    opts = _download_opts(cfg)
    cached_info = cfg.get("cached_info")

    with yt_dlp.YoutubeDL(opts) as ydl:
        if cached_info is not None:
            try:
                # Основной путь: переиспользуем уже извлечённые форматы —
                # ни одного нового обращения к YouTube на этот запрос.
                ydl.process_ie_result(dict(cached_info), download=True)
                return
            except Exception as e:
                # Кэш мог протухнуть (подписанные ссылки на файл живут
                # ограниченное время) — тихо откатываемся на обычное
                # извлечение+скачивание за один проход, как было раньше.
                print(f"[worker] cached_info не сработал ({e}), "
                      f"полное извлечение заново", file=sys.stderr)

        ydl.extract_info(cfg["url"], download=True)


def main() -> int:
    cfg = _read_config()
    mode = cfg.get("mode")
    try:
        if mode == "info":
            run_info(cfg)
        elif mode == "download":
            run_download(cfg)
        else:
            raise ValueError(f"Неизвестный mode: {mode!r}")
        return 0
    except Exception as e:
        # Тот же текст, что раньше печатал CLI yt-dlp в stderr — вся
        # существующая classify_error() в app.py матчит по подстрокам этого
        # текста (регистронезависимо), так что классификация ошибок и
        # детект 402/протухших cookies продолжают работать без изменений.
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""
Запуск FFmpeg/ffprobe с таймаутом и понятной ошибкой.

subprocess.run(check=True, capture_output=True) теряет stderr: в логах
остаётся только «returned non-zero exit status 1» и вся командная строка.
Здесь при ошибке в исключение попадает хвост stderr — по нему видно причину.

API:
    run_ffmpeg(cmd, timeout=900, what="ffmpeg") → CompletedProcess
"""

import subprocess


class FFmpegError(RuntimeError):
    pass


def run_ffmpeg(cmd: list[str], timeout: float = 900, what: str = "ffmpeg") -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"{what}: превышен таймаут {timeout:.0f}с") from exc
    except FileNotFoundError as exc:
        raise FFmpegError(f"{what}: {cmd[0]} не найден в PATH — установите FFmpeg") from exc
    if result.returncode != 0:
        tail = (result.stderr or b"").decode("utf-8", "replace").strip()[-800:]
        raise FFmpegError(f"{what}: код {result.returncode}\n{tail}")
    return result


def filter_path(path: str) -> str:
    """
    Путь к файлу для опции фильтра внутри одинарных кавычек: subtitles='{filter_path(p)}'.

    Два уровня разбора FFmpeg: сначала граф фильтров (кавычки, [],;), потом опции фильтра
    (':' разделяет опции, обратный слеш экранирует). Поэтому: слеши Windows → '/',
    ':' → '\\:', кавычка → '\\'' на уровне опций, а каждая кавычка уровня графа
    закрывает кавычки, экранируется и открывает их снова. Иначе путь вида
    C:/Users/O'Brien/subs.ass обрывает фильтр.
    """
    opt = path.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    return opt.replace("'", r"'\''")


def concat_line(path: str) -> str:
    """Строка списка concat-демультиплексора: file '<path>' с экранированной кавычкой."""
    p = str(path).replace("\\", "/").replace("'", r"'\''")
    return f"file '{p}'\n"

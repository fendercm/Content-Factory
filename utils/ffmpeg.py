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

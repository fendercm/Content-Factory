"""
Защита внешних вызовов (download_to, kie_client.upload_file — без сети) и экранирование
путей для FFmpeg (subtitles='…', concat-списки) — с настоящим ffmpeg, если он есть в PATH.
"""

import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from utils.ffmpeg import concat_line, filter_path

_ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 320
PlayResY: 240

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, \
Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, \
MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Hi
"""

# Имена папок, на которых старое экранирование (только ':') ломало фильтр
_TRICKY_DIRS = ["O'Brien", "with space, [x];y=z", "Мои 'файлы'"]
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg не найден в PATH")


def test_filter_path_escapes_drive_colon_backslashes_and_quotes():
    assert filter_path(r"C:\out\subs.ass") == r"C\:/out/subs.ass"
    assert filter_path(r"C:\O'Brien\s.ass") == r"C\:/O\'\''Brien/s.ass"


def test_concat_line_escapes_quote():
    assert concat_line(r"C:\a\it's.wav") == "file 'C:/a/it'\\''s.wav'\n"


def test_assembly_esc_delegates_to_filter_path():
    from agents.assembly_agent import _esc
    assert _esc(r"C:\O'Brien\s.ass") == filter_path(r"C:\O'Brien\s.ass")


@needs_ffmpeg
@pytest.mark.parametrize("dirname", _TRICKY_DIRS)
def test_subtitles_filter_accepts_tricky_path(tmp_path, dirname):
    d = tmp_path / dirname
    d.mkdir()
    ass = d / "sub's.ass"
    ass.write_text(_ASS, encoding="utf-8")
    r = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.2",
                        "-filter_complex", f"[0:v]subtitles='{filter_path(str(ass))}'[out]",
                        "-map", "[out]", "-f", "null", "-"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-300:]


@needs_ffmpeg
@pytest.mark.parametrize("dirname", _TRICKY_DIRS)
def test_concat_list_accepts_tricky_path(tmp_path, dirname):
    d = tmp_path / dirname
    d.mkdir()
    parts = []
    for i in range(2):
        w = d / f"part'{i}.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=0.2", str(w)],
                       check=True, timeout=60)
        parts.append(str(w))
    lst = d / "list.txt"
    lst.write_text("".join(concat_line(p) for p in parts), encoding="utf-8")
    out = d / "out.wav"
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", str(out)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and out.exists(), r.stderr[-300:]


def test_download_rejects_non_http_scheme(tmp_path):
    from utils.download import download_to
    with patch("utils.download.requests.get") as get:
        with pytest.raises(ValueError):
            download_to("file:///C:/secret.txt", str(tmp_path / "x"))
    get.assert_not_called()


def test_download_enforces_max_bytes(tmp_path):
    from utils.download import download_to
    resp = MagicMock(headers={}, **{"iter_content.return_value": [b"x" * 600, b"x" * 600]})
    resp.__enter__.return_value = resp
    with patch("utils.download.requests.get", return_value=resp):
        with pytest.raises(RuntimeError, match="лимит"):
            download_to("https://cdn.example/x.mp4", str(tmp_path / "x.mp4"), max_bytes=1000)
    assert not (tmp_path / "x.mp4").exists() and not (tmp_path / "x.mp4.part").exists()


@pytest.mark.parametrize("name", [".env", "pipeline.db", "script.json", "notes.txt"])
def test_kie_upload_refuses_non_media(tmp_path, name):
    from utils import kie_client
    f = tmp_path / name
    f.write_text("secret", encoding="utf-8")
    with patch("utils.kie_client.requests.post") as post:
        with pytest.raises(kie_client.KieError):
            kie_client.upload_file(f)
    post.assert_not_called()

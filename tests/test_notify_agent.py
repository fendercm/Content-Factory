"""Notify Agent: HTML-экранирование, токен не утекает в ошибки, запасные пути без фото и без ключей. Без сети."""

from unittest.mock import MagicMock

import pytest
import requests

from agents import notify_agent as na


def _ok(message_id=1):
    r = MagicMock()
    r.json.return_value = {"ok": True, "result": {"message_id": message_id}}
    return r


@pytest.fixture
def tg(monkeypatch):
    """Ключи Telegram заданы, requests.post подменён — сообщения никуда не уходят."""
    monkeypatch.setattr(na, "TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setattr(na, "TELEGRAM_CHAT_ID", "42")
    post = MagicMock(return_value=_ok())
    monkeypatch.setattr(na.requests, "post", post)
    return post


# ── экранирование ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("<b>&\"'", "&lt;b&gt;&amp;&quot;&#x27;"),
    ("", ""),
    (None, ""),
    (0, "0"),                                     # falsy число съедается — см. docs/tech/testing.md
    (42, "42"),
])
def test_esc(raw, expected):
    assert na._esc(raw) == expected


def test_alert_success_escapes_title_platforms_and_post_id(tg):
    na.alert_success("Тема <script>", ["youtube<x>"], "id&1", duration=61.25, status="<draft>")
    text = tg.call_args.kwargs["json"]["text"]
    assert "&lt;script&gt;" in text and "youtube&lt;x&gt;" in text and "id&amp;1" in text
    assert "<script>" not in text
    assert "61.2 сек" in text or "61.3 сек" in text
    assert "Статус: <b>&lt;draft&gt;</b>" in text
    assert text.startswith("✅ <b>Видео опубликовано!</b>")


def test_alert_success_dry_run_hides_status(tg):
    na.alert_success("T", ["youtube"], "skipped", dry_run=True, status="published")
    text = tg.call_args.kwargs["json"]["text"]
    assert "Dry-run" in text and "Статус" not in text


def test_alert_error_escapes_and_redacts(tg, monkeypatch):
    monkeypatch.setattr("utils.log_redact.redact", lambda s: s.replace("SECRET", "***"))
    na.alert_error("voice<agent>", "token SECRET <oops> " + "x" * 1000, attempt=2)
    text = tg.call_args.kwargs["json"]["text"]
    assert text.startswith("❌ ")
    assert "voice&lt;agent&gt;" in text and "&lt;oops&gt;" in text
    assert "SECRET" not in text and "Попытка: 2" in text
    assert len(text) < 600                         # причина обрезана до 400 символов


def test_alert_queued_escapes_title_and_path(tg, tmp_path):
    na.alert_queued("Серия <1>", 3, str(tmp_path / "нет<файла>.mp4"), duration=70)
    text = tg.call_args.kwargs["json"]["text"]
    assert "Серия &lt;1&gt;" in text and "нет&lt;файла&gt;.mp4" in text
    assert "series_queue.py reject 3" in text and "70.0 сек" in text


# ── send / send_photo / send_video ───────────────────────────────────────────

def test_send_without_credentials_does_not_call_api(monkeypatch):
    monkeypatch.setattr(na, "TELEGRAM_BOT_TOKEN", "")
    post = MagicMock()
    monkeypatch.setattr(na.requests, "post", post)
    assert na.send("привет") == {"ok": False, "message_id": None, "error": "credentials missing"}
    assert na.send_photo(b"x", "c")["error"] == "credentials missing"
    assert na.send_video("nope.mp4", "c")["error"] == "credentials missing"
    assert not post.called


def test_send_explicit_chat_id_and_level_icon(tg):
    out = na.send("текст", chat_id="777", level="warning")
    assert out == {"ok": True, "message_id": 1, "error": None}
    payload = tg.call_args.kwargs["json"]
    assert payload == {"chat_id": "777", "text": "⚠️ текст", "parse_mode": "HTML"}


def test_send_unknown_level_has_no_icon(tg):
    na.send("текст", level="debug")
    assert tg.call_args.kwargs["json"]["text"] == "текст"


def test_send_ok_false_returns_description(tg):
    tg.return_value.json.return_value = {"ok": False, "description": "chat not found"}
    assert na.send("x") == {"ok": False, "message_id": None, "error": "chat not found"}


def test_send_http_error_hides_token(tg, caplog):
    resp = MagicMock(status_code=400)
    resp.json.return_value = {"description": "Bad Request: can't parse entities"}
    tg.return_value.raise_for_status.side_effect = requests.HTTPError(
        "400 Client Error for url: https://api.telegram.org/bot123:SECRET/sendMessage", response=resp)
    out = na.send("x")
    assert out["error"] == "HTTP 400: Bad Request: can't parse entities"
    assert "SECRET" not in caplog.text


def test_send_connection_error_reports_type_only(tg):
    tg.side_effect = requests.ConnectionError("https://api.telegram.org/bot123:SECRET/sendMessage")
    assert na.send("x")["error"] == "ConnectionError"


def test_describe_error_response_without_json():
    resp = MagicMock(status_code=502)
    resp.json.side_effect = ValueError
    exc = requests.HTTPError("x", response=resp)
    assert na._describe_error(exc) == "HTTP 502"


def test_send_photo_failure_falls_back_to_text(tg):
    tg.side_effect = [requests.Timeout("slow"), _ok(9)]
    out = na.send_photo(b"jpg", "подпись")
    assert out["ok"] is True and out["message_id"] == 9
    assert tg.call_args_list[0].args[0].endswith("/sendPhoto")
    assert tg.call_args_list[1].args[0].endswith("/sendMessage")


def test_send_photo_ok_false(tg):
    tg.return_value.json.return_value = {"ok": False, "description": "too big"}
    assert na.send_photo(b"jpg", "c") == {"ok": False, "error": "too big"}


def test_send_video_error_returns_description(tg, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0" * 10)
    tg.side_effect = requests.ConnectionError("down")
    assert na.send_video(str(video), "c") == {"ok": False, "error": "ConnectionError"}


def test_send_video_ok_false(tg, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0" * 10)
    tg.return_value.json.return_value = {"ok": False, "description": "file too large"}
    assert na.send_video(str(video), "c") == {"ok": False, "error": "file too large"}


def test_send_video_over_limit_text_has_escaped_path(tg, tmp_path, monkeypatch):
    video = tmp_path / "a&b.mp4"
    video.write_bytes(b"0" * 10)
    monkeypatch.setattr(na, "TELEGRAM_VIDEO_LIMIT", 5)
    na.send_video(str(video), "<b>подпись</b>")
    text = tg.call_args.kwargs["json"]["text"]
    assert "a&amp;b.mp4" in text and text.startswith("ℹ️ <b>подпись</b>")


def test_send_video_missing_file_returns_error(tg):
    """Ролика нет на диске — ошибка в ответе, а не исключение (send_manual_upload ждёт {"ok": False})."""
    out = na.send_video("нет_такого.mp4", "c")
    assert out["ok"] is False and "нет_такого.mp4" in out["error"]


# ── send_manual_upload ───────────────────────────────────────────────────────

def test_send_manual_upload_escapes_platform_and_truncates_text(tg, tmp_path):
    video = tmp_path / "ep.mp4"
    video.write_bytes(b"0" * 10)
    out = na.send_manual_upload("Wibes<&>", str(video), "<i>" + "я" * 2000)
    assert out == {"ok": True, "message_id": 1}
    caption = tg.call_args.kwargs["data"]["caption"]
    assert caption.startswith("<b>Wibes&lt;&amp;&gt;: загрузить вручную</b>")
    code = caption.split("<code>", 1)[1].rsplit("</code>", 1)[0]
    assert code.startswith("&lt;i&gt;") and code.count("я") == 797   # 800 символов исходника до экранирования
    assert tg.call_args.kwargs["data"]["supports_streaming"] == "true"


# ── превью-кадр ──────────────────────────────────────────────────────────────

def test_alert_success_with_frame_sends_photo(tg, tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0")
    monkeypatch.setattr(na, "_extract_frame", lambda p: b"jpeg")
    na.alert_success("T", ["youtube"], "p1", video_path=str(video))
    assert tg.call_args.args[0].endswith("/sendPhoto")
    assert tg.call_args.kwargs["files"]["photo"][1] == b"jpeg"


def test_alert_success_without_frame_sends_text(tg, tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0")
    monkeypatch.setattr(na, "_extract_frame", lambda p: None)
    na.alert_success("T", ["youtube"], "p1", video_path=str(video))
    assert tg.call_args.args[0].endswith("/sendMessage")


def test_extract_frame_without_ffmpeg_or_file(monkeypatch, tmp_path):
    monkeypatch.setattr(na.shutil, "which", lambda name: None)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0")
    assert na._extract_frame(str(video)) is None
    monkeypatch.setattr(na.shutil, "which", lambda name: "ffmpeg")
    assert na._extract_frame(str(tmp_path / "missing.mp4")) is None


def test_extract_frame_reads_output_and_removes_temp(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0")
    seen = {}

    def fake_run(cmd, **kw):
        seen["out"] = cmd[-1]
        with open(cmd[-1], "wb") as f:
            f.write(b"JPEGDATA")

    monkeypatch.setattr(na.shutil, "which", lambda name: "ffmpeg")
    monkeypatch.setattr(na.subprocess, "run", fake_run)
    assert na._extract_frame(str(video)) == b"JPEGDATA"
    assert not __import__("os").path.exists(seen["out"])


def test_extract_frame_ffmpeg_failure_returns_none(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0")
    monkeypatch.setattr(na.shutil, "which", lambda name: "ffmpeg")

    def boom(cmd, **kw):
        raise na.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(na.subprocess, "run", boom)
    assert na._extract_frame(str(video)) is None

"""Publisher: ретраи только у идемпотентных шагов, POST /posts — ровно одна попытка, проверка ответа Zernio."""

from unittest.mock import MagicMock

import pytest
import requests

from agents import publisher_agent as pa

PRESIGN = {"uploadUrl": "https://storage.example/put/1", "publicUrl": "https://cdn.example/1.mp4"}


def _resp(json_data=None, status=200):
    r = MagicMock(status_code=status, ok=status < 400)
    r.json.return_value = json_data or {}
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}", response=r)
    return r


@pytest.fixture
def zernio(monkeypatch, tmp_path):
    """Ключ, файл ролика, сон без ожидания и подменённые requests.post/put — сеть не трогаем."""
    monkeypatch.setenv("ZERNIO_API_KEY", "test-key")
    monkeypatch.setattr(pa.time, "sleep", lambda s: None)
    video = tmp_path / "ep.mp4"
    video.write_bytes(b"0" * 2048)
    post, put = MagicMock(), MagicMock(return_value=_resp())
    monkeypatch.setattr(pa.requests, "post", post)
    monkeypatch.setattr(pa.requests, "put", put)
    return {"video": str(video), "post": post, "put": put, "tmp": tmp_path}


def _publish(z, platform="youtube", **kw):
    return pa.publish_video(z["video"], "Кабинет 7 · 1", "описание", ["психология"], platform, "acc1", **kw)


def _posts_calls(post):
    return [c for c in post.call_args_list if c.args[0].endswith("/posts")]


# ── POST /posts: одна попытка ──────────────────────────────────────────────────

@pytest.mark.parametrize("failure", [requests.Timeout("read timeout"), requests.ConnectionError("reset")])
def test_create_post_network_error_is_not_retried(zernio, failure):
    """Таймаут после отправки мог создать пост — повтор сделал бы дубль."""
    zernio["post"].side_effect = [_resp(PRESIGN), failure]
    with pytest.raises(type(failure)):
        _publish(zernio)
    assert len(_posts_calls(zernio["post"])) == 1


def test_create_post_5xx_is_not_retried_and_logged(zernio, caplog):
    zernio["post"].side_effect = [_resp(PRESIGN), _resp({"error": "boom"}, status=502)]
    with pytest.raises(requests.HTTPError):
        _publish(zernio)
    assert len(_posts_calls(zernio["post"])) == 1
    assert "Zernio POST /posts 502" in caplog.text and "boom" in caplog.text


def test_create_post_error_body_not_json_still_logged(zernio, caplog):
    bad = _resp(status=500)
    bad.json.side_effect = ValueError("not json")
    bad.text = "<html>Bad Gateway</html>"
    zernio["post"].side_effect = [_resp(PRESIGN), bad]
    with pytest.raises(requests.HTTPError):
        _publish(zernio)
    assert "Bad Gateway" in caplog.text


# ── presign / upload: ретраи ──────────────────────────────────────────────────

def test_presign_retries_5xx_then_succeeds(zernio):
    zernio["post"].side_effect = [_resp(status=503), _resp(PRESIGN), _resp({"_id": "p1", "status": "published"})]
    out = _publish(zernio)
    assert out == {"post_id": "p1", "status": "published", "platform": "youtube"}
    assert zernio["post"].call_count == 3


def test_presign_4xx_not_retried(zernio):
    zernio["post"].side_effect = [_resp(status=401)]
    with pytest.raises(requests.HTTPError):
        _publish(zernio)
    assert zernio["post"].call_count == 1 and not zernio["put"].called


def test_upload_connection_error_retried_until_limit(zernio):
    zernio["post"].side_effect = [_resp(PRESIGN)]
    zernio["put"].side_effect = requests.ConnectionError("reset")
    with pytest.raises(requests.ConnectionError):
        _publish(zernio)
    assert zernio["put"].call_count == pa._UPLOAD_ATTEMPTS
    assert _posts_calls(zernio["post"]) == []          # без загруженного файла пост не создаём


def test_with_retries_backoff_grows_linearly(monkeypatch):
    sleeps = []
    monkeypatch.setattr(pa.time, "sleep", sleeps.append)
    fn = MagicMock(side_effect=[requests.Timeout(), requests.Timeout(), "ok"])
    assert pa._with_retries("x", fn) == "ok"
    assert sleeps == [pa._UPLOAD_BACKOFF, pa._UPLOAD_BACKOFF * 2]


def test_with_retries_http_error_without_response_retried(monkeypatch):
    monkeypatch.setattr(pa.time, "sleep", lambda s: None)
    fn = MagicMock(side_effect=[requests.HTTPError("no response"), "ok"])
    assert pa._with_retries("x", fn) == "ok"


# ── валидация ответа POST /posts ─────────────────────────────────────────────

@pytest.mark.parametrize("body", [{}, {"post": {"status": "published"}}, {"_id": "", "status": "queued"}])
def test_missing_post_id_raises(zernio, body):
    zernio["post"].side_effect = [_resp(PRESIGN), _resp(body)]
    with pytest.raises(RuntimeError, match="не вернул id поста"):
        _publish(zernio)


@pytest.mark.parametrize("status", ["failed", "ERROR", "rejected", "Cancelled", "canceled"])
def test_failed_status_raises(zernio, status):
    zernio["post"].side_effect = [_resp(PRESIGN), _resp({"post": {"id": "p9", "status": status}})]
    with pytest.raises(RuntimeError, match="p9"):
        _publish(zernio)


def test_draft_status_returned_with_warning(zernio, caplog):
    zernio["post"].side_effect = [_resp(PRESIGN), _resp({"_id": "p2", "status": "draft"})]
    assert _publish(zernio)["status"] == "draft"
    assert "черновик" in caplog.text


def test_id_field_used_when_no_underscore_id(zernio):
    zernio["post"].side_effect = [_resp(PRESIGN), _resp({"id": "p3"})]
    assert _publish(zernio) == {"post_id": "p3", "status": "unknown", "platform": "youtube"}


# ── входные данные и обложка ─────────────────────────────────────────────────

def test_empty_account_id_raises_before_network(zernio):
    with pytest.raises(ValueError, match="account_id"):
        pa.publish_video(zernio["video"], "T", "D", [], "youtube", "")
    assert not zernio["post"].called


def test_thumbnail_failure_does_not_block_publish(zernio):
    thumb = zernio["tmp"] / "t.jpg"
    thumb.write_bytes(b"jpg")
    zernio["post"].side_effect = [_resp(PRESIGN), requests.ConnectionError("thumb"), _resp({"_id": "p4"})]
    assert _publish(zernio, thumbnail_path=str(thumb))["post_id"] == "p4"
    body = _posts_calls(zernio["post"])[0].kwargs["json"]
    assert "thumbnailUrl" not in body["platforms"][0].get("platformSpecificData", {})


def test_thumbnail_ignored_for_non_youtube(zernio):
    thumb = zernio["tmp"] / "t.jpg"
    thumb.write_bytes(b"jpg")
    zernio["post"].side_effect = [_resp(PRESIGN), _resp({"_id": "p5"})]
    _publish(zernio, platform="instagram", thumbnail_path=str(thumb))
    assert zernio["post"].call_count == 2               # второго presign для обложки нет


def test_tiktok_privacy_level_from_env(monkeypatch):
    monkeypatch.setenv("TIKTOK_PRIVACY_LEVEL", "SELF_ONLY")
    assert pa._tiktok_settings(False)["privacy_level"] == "SELF_ONLY"
    assert pa._tiktok_settings(False)["video_made_with_ai"] is False


def test_headers_never_sent_without_key(monkeypatch, tmp_path):
    monkeypatch.setenv("ZERNIO_API_KEY", "")
    post = MagicMock()
    monkeypatch.setattr(pa.requests, "post", post)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0")
    with pytest.raises(EnvironmentError):
        pa.publish_video(str(video), "T", "D", [], "youtube", "acc1")
    assert not post.called

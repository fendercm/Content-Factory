import pytest
from unittest.mock import MagicMock, patch, mock_open

from agents.publisher_agent import publish_video, _headers


FAKE_PRESIGN = {
    "uploadUrl": "https://storage.zernio.com/presign/abc123",
    "publicUrl": "https://cdn.zernio.com/media/abc123.mp4",
}
FAKE_POST = {
    "_id": "post_xyz",
    "status": "scheduled",
}


def _mock_response(json_data=None, status_code=200):
    m = MagicMock()
    m.status_code = status_code
    m.ok = status_code < 400
    m.json.return_value = json_data or {}
    m.raise_for_status = MagicMock()
    return m


# ── _headers ────────────────────────────────────────────────────────────────

@patch.dict("os.environ", {"ZERNIO_API_KEY": "test-key"})
def test_headers_returns_bearer():
    h = _headers()
    assert h["Authorization"] == "Bearer test-key"
    assert h["Content-Type"] == "application/json"


def test_headers_missing_key_raises():
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(EnvironmentError, match="ZERNIO_API_KEY"):
            _headers()


# ── publish_video validation ──────────────────────────────────────────────────

@patch.dict("os.environ", {"ZERNIO_API_KEY": "test-key"})
def test_publish_video_missing_file_raises():
    with pytest.raises(ValueError, match="Видео файл не найден"):
        publish_video("/nonexistent/video.mp4", "title", "desc", [], "tiktok", "acc1")


# ── Happy path ────────────────────────────────────────────────────────────────

@patch.dict("os.environ", {"ZERNIO_API_KEY": "test-key"})
@patch("agents.publisher_agent.os.path.getsize", return_value=1024 * 1024 * 10)
@patch("agents.publisher_agent.os.path.exists", return_value=True)
@patch("agents.publisher_agent.requests.put")
@patch("agents.publisher_agent.requests.post")
def test_publish_video_returns_required_keys(mock_post, mock_put, mock_exists, mock_size):
    mock_post.side_effect = [
        _mock_response(FAKE_PRESIGN),
        _mock_response(FAKE_POST),
    ]
    mock_put.return_value = _mock_response()

    with patch("builtins.open", mock_open(read_data=b"fake-video")):
        result = publish_video(
            "/fake/video.mp4", "Заголовок", "Описание", ["тренды"], "tiktok", "acc1"
        )

    assert {"post_id", "status", "platform"} <= result.keys()
    assert result["post_id"] == "post_xyz"
    assert result["status"] == "scheduled"
    assert result["platform"] == "tiktok"


@patch.dict("os.environ", {"ZERNIO_API_KEY": "test-key"})
@patch("agents.publisher_agent.os.path.getsize", return_value=1024)
@patch("agents.publisher_agent.os.path.exists", return_value=True)
@patch("agents.publisher_agent.requests.put")
@patch("agents.publisher_agent.requests.post")
def test_publish_video_wrapped_post_response(mock_post, mock_put, mock_exists, mock_size):
    """Zernio может вернуть {"post": {...}} — результат разворачивается."""
    mock_post.side_effect = [
        _mock_response(FAKE_PRESIGN),
        _mock_response({"post": FAKE_POST}),
    ]
    mock_put.return_value = _mock_response()

    with patch("builtins.open", mock_open(read_data=b"data")):
        result = publish_video("/fake/v.mp4", "T", "D", [], "tiktok", "acc1")

    assert result["post_id"] == "post_xyz"


@patch.dict("os.environ", {"ZERNIO_API_KEY": "test-key"})
@patch("agents.publisher_agent.os.path.getsize", return_value=1024)
@patch("agents.publisher_agent.os.path.exists", return_value=True)
@patch("agents.publisher_agent.requests.put")
@patch("agents.publisher_agent.requests.post")
def test_publish_video_calls_presign_then_upload_then_post(mock_post, mock_put, mock_exists, mock_size):
    mock_post.side_effect = [_mock_response(FAKE_PRESIGN), _mock_response(FAKE_POST)]
    mock_put.return_value = _mock_response()

    with patch("builtins.open", mock_open(read_data=b"data")):
        publish_video("/fake/v.mp4", "T", "D", [], "instagram", "acc1")

    assert mock_post.call_count == 2
    presign_call_url = mock_post.call_args_list[0][0][0]
    post_call_url = mock_post.call_args_list[1][0][0]
    assert presign_call_url.endswith("/media/presign")
    assert post_call_url.endswith("/posts")
    mock_put.assert_called_once_with(
        FAKE_PRESIGN["uploadUrl"],
        data=mock_put.call_args[1]["data"],
        headers={"Content-Type": "video/mp4"},
        timeout=300,
    )


@patch.dict("os.environ", {"ZERNIO_API_KEY": "test-key"})
@patch("agents.publisher_agent.os.path.getsize", return_value=1024)
@patch("agents.publisher_agent.os.path.exists", return_value=True)
@patch("agents.publisher_agent.requests.put")
@patch("agents.publisher_agent.requests.post")
def test_publish_video_post_body_has_hashtags(mock_post, mock_put, mock_exists, mock_size):
    mock_post.side_effect = [_mock_response(FAKE_PRESIGN), _mock_response(FAKE_POST)]
    mock_put.return_value = _mock_response()

    with patch("builtins.open", mock_open(read_data=b"data")):
        publish_video("/fake/v.mp4", "T", "D", ["лайфхак", "тренды"], "tiktok", "acc1")

    post_body = mock_post.call_args_list[1][1]["json"]
    assert post_body["hashtags"] == ["лайфхак", "тренды"]


@pytest.mark.parametrize("platform", ["tiktok", "youtube", "instagram", "linkedin"])
@patch.dict("os.environ", {"ZERNIO_API_KEY": "test-key"})
@patch("agents.publisher_agent.os.path.getsize", return_value=1024)
@patch("agents.publisher_agent.os.path.exists", return_value=True)
@patch("agents.publisher_agent.requests.put")
@patch("agents.publisher_agent.requests.post")
def test_publish_video_title_and_platform_entry(mock_post, mock_put, mock_exists, mock_size, platform):
    mock_post.side_effect = [_mock_response(FAKE_PRESIGN), _mock_response(FAKE_POST)]
    mock_put.return_value = _mock_response()

    with patch("builtins.open", mock_open(read_data=b"data")):
        publish_video("/fake/v.mp4", "Заголовок", "Описание", [], platform, "acc1")

    post_body = mock_post.call_args_list[1][1]["json"]
    assert post_body["title"] == "Заголовок"
    assert post_body["content"] == "Описание"
    assert post_body["publishNow"] is True

    platform_entry = post_body["platforms"][0]
    assert platform_entry["platform"] == platform
    assert platform_entry["accountId"] == "acc1"


@patch.dict("os.environ", {"ZERNIO_API_KEY": "test-key"})
@patch("agents.publisher_agent.os.path.getsize", return_value=1024)
@patch("agents.publisher_agent.os.path.exists", return_value=True)
@patch("agents.publisher_agent.requests.put")
@patch("agents.publisher_agent.requests.post")
def test_publish_video_uses_media_url_from_presign(mock_post, mock_put, mock_exists, mock_size):
    mock_post.side_effect = [_mock_response(FAKE_PRESIGN), _mock_response(FAKE_POST)]
    mock_put.return_value = _mock_response()

    with patch("builtins.open", mock_open(read_data=b"data")):
        publish_video("/fake/v.mp4", "T", "D", [], "instagram", "acc1")

    post_body = mock_post.call_args_list[1][1]["json"]
    assert post_body["mediaItems"][0]["url"] == FAKE_PRESIGN["publicUrl"]


@patch.dict("os.environ", {"ZERNIO_API_KEY": "test-key"})
@patch("agents.publisher_agent.os.path.getsize", return_value=1024)
@patch("agents.publisher_agent.os.path.exists", return_value=True)
@patch("agents.publisher_agent.requests.put")
@patch("agents.publisher_agent.requests.post")
def test_publish_video_youtube_thumbnail_in_platform_specific_data(
    mock_post, mock_put, mock_exists, mock_size
):
    """YouTube + thumbnail → второй presign и platformSpecificData.thumbnailUrl."""
    thumb_presign = {
        "uploadUrl": "https://storage.zernio.com/presign/thumb1",
        "publicUrl": "https://cdn.zernio.com/media/thumb1.jpg",
    }
    mock_post.side_effect = [
        _mock_response(FAKE_PRESIGN),
        _mock_response(thumb_presign),
        _mock_response(FAKE_POST),
    ]
    mock_put.return_value = _mock_response()

    with patch("builtins.open", mock_open(read_data=b"data")):
        publish_video(
            "/fake/v.mp4", "T", "D", [], "youtube", "acc1",
            thumbnail_path="/fake/thumb.jpg",
        )

    assert mock_post.call_count == 3
    post_body = mock_post.call_args_list[2][1]["json"]
    platform_entry = post_body["platforms"][0]
    assert platform_entry["platformSpecificData"]["thumbnailUrl"] == thumb_presign["publicUrl"]

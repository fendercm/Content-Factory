"""VK Publisher: тело video.save, ключ не в URL, ошибки загрузки и ответы без video_id — без сети."""

from unittest.mock import MagicMock

import pytest
import requests

from agents import vk_publisher as vk

TOKEN = "vk1.a.secret-token-value"


def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    return r


@pytest.fixture
def vk_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VK_USER_TOKEN", TOKEN)
    monkeypatch.setenv("VK_GROUP_ID", "123")
    monkeypatch.setattr(vk.time, "sleep", lambda s: None)
    post = MagicMock()
    monkeypatch.setattr(vk.requests, "post", post)
    video = tmp_path / "ep.mp4"
    video.write_bytes(b"\x00" * 16)
    return {"video": str(video), "post": post}


def test_save_params_and_token_only_in_body(vk_env):
    vk_env["post"].side_effect = [_resp({"response": {"upload_url": "https://up/vk", "video_id": 7}}),
                                  _resp({"size": 16})]
    out = vk.publish_video(vk_env["video"], "Т" * 200, "описание", ["#психология", "кабинет7"])
    save = vk_env["post"].call_args_list[0]
    assert save.args[0] == vk.API_URL + "video.save" and TOKEN not in save.args[0]
    data = save.kwargs["data"]
    assert data["access_token"] == TOKEN and data["v"] == vk.API_VERSION
    assert data["name"] == "Т" * 128 and data["group_id"] == "123"
    assert data["description"] == "описание\n\n#психология #кабинет7"
    assert data["wallpost"] == 1 and data["repeat"] == 0
    # owner_id нет ни в одном ответе — берётся сообщество
    assert out == {"post_id": "-123_7", "status": "published", "platform": "vk", "url": "https://vk.com/video-123_7"}


def test_upload_ids_take_priority(vk_env):
    vk_env["post"].side_effect = [_resp({"response": {"upload_url": "https://up/vk", "video_id": 7, "owner_id": -1}}),
                                  _resp({"video_id": 8, "owner_id": -2})]
    assert vk.publish_video(vk_env["video"], "t", "", [])["post_id"] == "-2_8"


def test_empty_description_and_tags_gives_empty_text(vk_env):
    vk_env["post"].side_effect = [_resp({"response": {"upload_url": "u", "video_id": 1}}), _resp({})]
    vk.publish_video(vk_env["video"], "t", "", [])
    assert vk_env["post"].call_args_list[0].kwargs["data"]["description"] == ""


def test_no_video_id_anywhere_raises(vk_env):
    vk_env["post"].side_effect = [_resp({"response": {"upload_url": "u"}}), _resp({"size": 1})]
    with pytest.raises(vk.VkError, match="без video_id"):
        vk.publish_video(vk_env["video"], "t", "d", [])


def test_upload_error_body_raises_without_retry(vk_env):
    vk_env["post"].side_effect = [_resp({"response": {"upload_url": "u", "video_id": 1}}),
                                  _resp({"error": "file too big"})]
    with pytest.raises(vk.VkError, match="file too big"):
        vk.publish_video(vk_env["video"], "t", "d", [])
    assert vk_env["post"].call_count == 2


def test_upload_gives_up_after_attempts(vk_env):
    vk_env["post"].side_effect = requests.Timeout("slow")
    with pytest.raises(requests.Timeout):
        vk._upload("https://up/vk", vk_env["video"], attempts=2)
    assert vk_env["post"].call_count == 2


def test_upload_zero_attempts_raises_vk_error(vk_env):
    with pytest.raises(vk.VkError, match="нет попыток"):
        vk._upload("https://up/vk", vk_env["video"], attempts=0)


def test_unknown_error_code_has_no_hint(vk_env):
    vk_env["post"].return_value = _resp({"error": {"error_code": 100, "error_msg": "One of the parameters"}})
    with pytest.raises(vk.VkError) as exc:
        vk._call("video.save")
    assert str(exc.value) == "VK video.save: 100 One of the parameters"


def test_call_http_error_has_no_token(vk_env):
    r = _resp({})
    r.raise_for_status.side_effect = requests.HTTPError("502 Bad Gateway for url: " + vk.API_URL + "video.save")
    vk_env["post"].return_value = r
    with pytest.raises(requests.HTTPError) as exc:
        vk._call("video.save")
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize("token,group,expected", [("t", "1", True), ("", "1", False), ("t", "", False)])
def test_is_configured(monkeypatch, token, group, expected):
    monkeypatch.setenv("VK_USER_TOKEN", token)
    monkeypatch.setenv("VK_GROUP_ID", group)
    assert vk.is_configured() is expected

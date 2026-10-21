"""VK Publisher без сети: video.save + загрузка файла, ошибки доступа, повтор загрузки, сериал без VK."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from agents import vk_publisher as vk


def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


@pytest.fixture
def vk_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VK_USER_TOKEN", "vk1.a.secret-token-value")
    monkeypatch.setenv("VK_GROUP_ID", "-123")
    video = tmp_path / "ep.mp4"
    video.write_bytes(b"\x00" * 16)
    return str(video)


def test_not_configured_without_env(monkeypatch):
    monkeypatch.delenv("VK_USER_TOKEN", raising=False)
    monkeypatch.delenv("VK_GROUP_ID", raising=False)
    assert vk.is_configured() is False
    with pytest.raises(vk.VkError):
        vk.publish_video("x.mp4", "t", "d", [])


def test_publish_saves_once_and_uploads(vk_env):
    save = _resp({"response": {"upload_url": "https://upload.example/vk", "video_id": 456, "owner_id": -123}})
    upload = _resp({"owner_id": -123, "video_id": 456, "size": 16})
    with patch.object(vk.requests, "post", side_effect=[save, upload]) as post:
        res = vk.publish_video(vk_env, "Кабинет 7 · 1. Тринадцатый курс", "Описание", ["психология", "#сериал"])
    assert res == {"post_id": "-123_456", "status": "published", "platform": "vk",
                   "url": "https://vk.com/video-123_456"}
    (save_url,), save_kw = post.call_args_list[0]
    assert save_url.endswith("video.save") and "secret" not in save_url   # ключ не в URL
    assert save_kw["data"]["group_id"] == "123" and save_kw["data"]["wallpost"] == 1
    assert save_kw["data"]["description"].endswith("#психология #сериал")
    assert post.call_args_list[1].args[0] == "https://upload.example/vk"


def test_access_error_has_hint_and_skips_upload(vk_env):
    err = _resp({"error": {"error_code": 15, "error_msg": "Access denied"}})
    with patch.object(vk.requests, "post", side_effect=[err]) as post:
        with pytest.raises(vk.VkError, match="права video"):
            vk.publish_video(vk_env, "t", "d", [])
    assert post.call_count == 1


def test_upload_retries_network_error_without_new_save(vk_env):
    save = _resp({"response": {"upload_url": "https://upload.example/vk", "video_id": 7, "owner_id": -123}})
    ok = _resp({"owner_id": -123, "video_id": 7})
    with patch.object(vk.requests, "post", side_effect=[save, requests.ConnectionError("reset"), ok]) as post, \
         patch.object(vk.time, "sleep"):
        res = vk.publish_video(vk_env, "t", "d", [])
    assert res["post_id"] == "-123_7"
    assert sum(1 for c in post.call_args_list if c.args[0].endswith("video.save")) == 1


_EXTRA_HARNESS = """
import json, os, sys
from unittest.mock import patch
sys.path.insert(0, %(root)r)
sys.argv = ["main.py"]
os.environ["DB_PATH"] = %(db)r
os.environ["VK_USER_TOKEN"] = "vk1.a.secret-token-value"
os.environ["VK_GROUP_ID"] = "123"
for _k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ZERNIO_API_KEY"):
    os.environ[_k] = ""  # до load_dotenv в main: настоящие ключи из .env не подхватятся (см. conftest.py)
import main
from agents import vk_publisher as vk
series = {"also_publish": ["vk"], "hashtags": []}
video = {"video_path": "ep.mp4"}
out = {"dry": main._publish_series_extra(series, video, "t", "d", dry_run=True)}
with patch.object(vk, "publish_video", side_effect=vk.VkError("VK video.save: 15 Access denied")), \
     patch.object(main, "_alert") as alert:
    out["failed"] = main._publish_series_extra(series, video, "t", "d", dry_run=False)
    out["alerts"] = alert.call_count
del os.environ["VK_USER_TOKEN"]
out["unconfigured"] = main._publish_series_extra(series, video, "t", "d", dry_run=False)
open(%(result)r, "w", encoding="utf-8").write(json.dumps(out))
"""


def test_series_extra_skips_unconfigured_and_alerts_on_failure(tmp_path):
    # main подменяет sys.stdout при импорте — запускаем его в отдельном процессе, как в test_audit_fixes
    root = Path(__file__).resolve().parent.parent
    result = tmp_path / "result.json"
    code = _EXTRA_HARNESS % {"root": str(root), "db": str(tmp_path / "p.db"), "result": str(result)}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, cwd=str(tmp_path), timeout=300)
    assert result.exists(), proc.stderr.decode("utf-8", "replace")[-3000:]
    out = json.loads(result.read_text(encoding="utf-8"))
    assert out["dry"] == {"vk": {"status": "skipped"}}
    assert out["failed"]["vk"]["status"] == "failed" and out["alerts"] == 1
    assert out["unconfigured"] == {"vk": {"status": "not_configured"}}

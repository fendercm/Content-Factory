"""Площадки без API (Wibes): ролик и подпись уходят в Telegram для ручной загрузки."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from agents import notify_agent as na

ROOT = Path(__file__).resolve().parent.parent


def _creds():
    return patch.multiple(na, TELEGRAM_BOT_TOKEN="123:abc", TELEGRAM_CHAT_ID="42")


def test_send_manual_upload_sends_video_with_copyable_caption(tmp_path):
    video = tmp_path / "ep.mp4"
    video.write_bytes(b"0" * 1000)
    resp = MagicMock()
    resp.json.return_value = {"ok": True, "result": {"message_id": 7}}
    with _creds(), patch.object(na.requests, "post", return_value=resp) as post:
        out = na.send_manual_upload("Wibes", str(video), "Кабинет 7 · 1. Тест <финал>\n\n#психология")
    assert out == {"ok": True, "message_id": 7}
    assert post.call_args.args[0].endswith("/sendVideo")
    caption = post.call_args.kwargs["data"]["caption"]
    assert "<code>" in caption and "&lt;финал&gt;" in caption  # HTML экранирован
    assert "video" in post.call_args.kwargs["files"]


def test_send_video_over_limit_falls_back_to_text(tmp_path):
    video = tmp_path / "big.mp4"
    video.write_bytes(b"0" * 10)
    with _creds(), patch.object(na, "TELEGRAM_VIDEO_LIMIT", 5), \
         patch.object(na, "send", return_value={"ok": True}) as send, \
         patch.object(na.requests, "post") as post:
        na.send_video(str(video), "подпись")
    assert send.called and not post.called


_HARNESS = """
import json, os, sys
from unittest.mock import patch
sys.path.insert(0, %(root)r)
sys.argv = ["main.py"]
os.environ["DB_PATH"] = %(db)r
for _k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ZERNIO_API_KEY"):
    os.environ[_k] = ""  # до load_dotenv в main: настоящие ключи из .env не подхватятся (см. conftest.py)
import main
from agents import notify_agent
series = {"manual_publish": ["Wibes", "VK Клипы"], "hashtags": ["психология"]}
video = {"video_path": "ep.mp4"}
out = {}
with patch.object(notify_agent, "send_manual_upload", return_value={"ok": True}) as up:
    out["ok"] = main._publish_series_extra(series, video, "t", "описание", dry_run=False)
    out["label"], out["text"] = up.call_args.args[0], up.call_args.args[2]
with patch.object(notify_agent, "send_manual_upload", return_value={"ok": False, "error": "403"}), \\
     patch.object(main, "_alert") as alert:
    out["fail"] = main._publish_series_extra(series, video, "t", "описание", dry_run=False)
    out["alerts"] = alert.call_count
open(%(result)r, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False))
"""


def test_series_manual_platforms_get_one_telegram_message(tmp_path):
    result = tmp_path / "r.json"
    code = _HARNESS % {"root": str(ROOT), "db": str(tmp_path / "p.db"), "result": str(result)}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, cwd=str(tmp_path), timeout=300)
    assert result.exists(), proc.stderr.decode("utf-8", "replace")[-3000:]
    out = json.loads(result.read_text(encoding="utf-8"))
    assert out["ok"] == {"manual": {"status": "manual", "platforms": ["Wibes", "VK Клипы"]}}
    assert out["label"] == "Wibes, VK Клипы"
    assert out["text"] == "описание\n\n#психология"
    assert out["fail"]["manual"]["status"] == "failed" and out["alerts"] == 1

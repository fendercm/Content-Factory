"""Защита внешних вызовов: download_to и kie_client.upload_file (без сети)."""

from unittest.mock import MagicMock, patch

import pytest


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

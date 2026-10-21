"""Тело POST /posts в Zernio: метки ИИ (YouTube/TikTok) и обязательные tiktokSettings."""

from unittest.mock import MagicMock, patch

from agents import publisher_agent as pa


def _post_body(platform, ai_generated):
    resp = MagicMock(ok=True)
    resp.json.return_value = {"post": {"_id": "p1", "status": "published"}}
    with patch.object(pa.requests, "post", return_value=resp) as post, \
         patch.object(pa, "_headers", return_value={}):
        pa._create_post("https://media/v.mp4", "v.mp4", 10, "video/mp4", "Кабинет 7 · 1. Тест", "описание",
                        ["психология"], platform, "acc1", ai_generated=ai_generated)
    return post.call_args.kwargs["json"]


def test_youtube_series_marked_synthetic():
    entry = _post_body("youtube", ai_generated=True)["platforms"][0]
    assert entry["platformSpecificData"]["containsSyntheticMedia"] is True


def test_youtube_clips_not_marked():
    entry = _post_body("youtube", ai_generated=False)["platforms"][0]
    assert "containsSyntheticMedia" not in entry.get("platformSpecificData", {})


def test_tiktok_has_required_settings_and_ai_flag(monkeypatch):
    monkeypatch.delenv("TIKTOK_PRIVACY_LEVEL", raising=False)
    settings = _post_body("tiktok", ai_generated=True)["platforms"][0]["platformSpecificData"]["tiktokSettings"]
    for field in ("privacy_level", "allow_comment", "allow_duet", "allow_stitch",
                  "content_preview_confirmed", "express_consent_given"):
        assert field in settings
    assert settings["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert settings["content_preview_confirmed"] is True and settings["express_consent_given"] is True
    assert settings["video_made_with_ai"] is True

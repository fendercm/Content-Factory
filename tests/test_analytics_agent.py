"""Analytics Agent без сети: разбор ответов Zernio /analytics и YouTube Data API, upsert в performance."""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from agents import analytics_agent as aa

# Пост из GET /posts (сокращённо, форма как у реального ответа Zernio)
POST = {
    "_id": "6ada5777bb48d28fc6545c34",
    "title": "Кабинет 7 · 2. Четыре килограмма тишины",
    "createdAt": "2026-10-22T18:35:35.300Z",
    "platforms": [{
        "platform": "youtube",
        "accountId": {"_id": "6a00f3c092b3d8e85fb8669a", "platform": "youtube"},
        "status": "published",
        "publishedAt": "2026-10-22T18:35:38.587Z",
        "platformPostId": "Bce2m38XWtM",
    }],
}

_ZERO = {"impressions": 0, "reach": 0, "likes": 0, "comments": 0, "shares": 0, "views": 0,
         "engagementRate": 0, "lastUpdated": None}

# GET /analytics?postId=… → 202, пока Zernio не синхронизировал статистику с YouTube
PENDING = {
    "postId": POST["_id"],
    "status": "published",
    "analytics": dict(_ZERO),
    "platformAnalytics": [{"platform": "youtube", "platformPostId": "Bce2m38XWtM",
                           "analytics": None, "syncStatus": "pending"}],
    "syncStatus": "pending",
    "message": "Analytics are being synced from the platform. Please try again in a few moments.",
}

SYNCED = {
    **PENDING,
    "analytics": {**_ZERO, "views": 1231, "impressions": 4000, "likes": 16, "comments": 2,
                  "lastUpdated": "2026-10-23T08:30:00Z"},
    "platformAnalytics": [{"platform": "youtube", "platformPostId": "Bce2m38XWtM",
                           "analytics": {"views": 1231, "likes": 16, "comments": 2}, "syncStatus": "synced"}],
    "syncStatus": "synced",
    "message": None,
}

# YouTube Data API videos.list?part=statistics
YT_RESPONSE = {"items": [{"id": "Bce2m38XWtM", "statistics": {
    "viewCount": "1231", "likeCount": "16", "favoriteCount": "0", "commentCount": "0"}}]}


def _resp(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def test_pending_zernio_is_no_data_not_zero():
    """Нули из несинхронизированного ответа — «нет данных», а не 0 просмотров."""
    assert aa._zernio_metrics(PENDING) is None
    row = aa._parse_post(POST, PENDING)
    assert row["views"] is None and row["likes"] is None and row["comments"] is None
    assert row["metrics_source"] is None
    assert row["platform"] == "youtube"


def test_synced_zernio_metrics_parsed():
    row = aa._parse_post(POST, SYNCED)
    assert (row["views"], row["likes"], row["comments"]) == (1231, 16, 2)
    assert row["metrics_source"] == "zernio"


def test_synced_platform_used_when_summary_empty():
    resp = {**PENDING, "platformAnalytics": SYNCED["platformAnalytics"]}
    assert aa._zernio_metrics(resp) == {"views": 1231, "likes": 16, "comments": 2}


def test_youtube_stats_take_priority_over_pending_zernio():
    stats = {"Bce2m38XWtM": {"views": 1231, "likes": 16, "comments": 0}}
    row = aa._parse_post(POST, PENDING, stats)
    assert (row["views"], row["likes"], row["comments"]) == (1231, 16, 0)
    assert row["metrics_source"] == "youtube"


def test_fetch_youtube_stats_parses_videos_list(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    with patch.object(aa.requests, "get", return_value=_resp(YT_RESPONSE)) as get:
        stats = aa._fetch_youtube_stats(["Bce2m38XWtM"])
    assert stats == {"Bce2m38XWtM": {"views": 1231, "likes": 16, "comments": 0}}
    # Ключ — в заголовке, не в URL
    assert get.call_args.kwargs["headers"] == {"X-Goog-Api-Key": "test-key"}
    assert "key" not in get.call_args.kwargs["params"]


def test_fetch_youtube_stats_without_key(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "")
    with patch.object(aa.requests, "get") as get:
        assert aa._fetch_youtube_stats(["Bce2m38XWtM"]) == {}
    get.assert_not_called()


def test_fetch_post_analytics_uses_analytics_endpoint(monkeypatch):
    """GET /posts/{id}/analytics в Zernio нет (404) — правильный путь /analytics?postId=…"""
    monkeypatch.setenv("ZERNIO_API_KEY", "test-key")
    with patch.object(aa.requests, "get", return_value=_resp(PENDING, status=202)) as get:
        assert aa._fetch_post_analytics(POST["_id"]) == PENDING
    assert get.call_args.args[0] == f"{aa.BASE_URL}/analytics"
    assert get.call_args.kwargs["params"] == {"postId": POST["_id"]}


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "p.db")
    aa._ensure_table(conn)
    yield conn
    conn.close()


def test_upsert_no_data_keeps_previous_metrics(db):
    stats = {"Bce2m38XWtM": {"views": 1231, "likes": 16, "comments": 0}}
    aa._upsert_performance(db, aa._parse_post(POST, PENDING, stats))
    aa._upsert_performance(db, aa._parse_post(POST, PENDING))  # Zernio pending, YouTube недоступен
    assert db.execute("SELECT views, likes, comments FROM performance").fetchone() == (1231, 16, 0)


def test_upsert_new_post_without_data_inserts_zeros(db):
    aa._upsert_performance(db, aa._parse_post(POST, PENDING))
    assert db.execute("SELECT views, likes, comments FROM performance").fetchone() == (0, 0, 0)


def test_run_writes_youtube_numbers(monkeypatch, tmp_path):
    monkeypatch.setenv("ZERNIO_ACCOUNT_ID", "6a00f3c092b3d8e85fb8669a")
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    post = {**POST, "createdAt": "2999-01-01T00:00:00Z"}  # всегда внутри окна --days
    with patch.object(aa, "_fetch_posts", return_value=[post]), \
         patch.object(aa, "_fetch_post_analytics") as zernio, \
         patch.object(aa.requests, "get", return_value=_resp(YT_RESPONSE)):
        top = aa.run(days=7, db_path=str(tmp_path / "p.db"))
    zernio.assert_not_called()  # YouTube уже дал цифры — Zernio не спрашиваем
    assert top[0]["views"] == 1231 and top[0]["likes"] == 16

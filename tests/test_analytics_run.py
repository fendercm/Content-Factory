"""Analytics Agent: загрузка постов, окно по датам, ошибки Zernio/YouTube и топ-3 — без сети, БД во временной папке."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
import requests

from agents import analytics_agent as aa


def _resp(payload, status=200):
    r = MagicMock(status_code=status)
    r.json.return_value = payload
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}", response=r)
    return r


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _synced(views, likes=0, comments=0):
    return {"syncStatus": "synced", "analytics": {"views": views, "likes": likes, "comments": comments}}


@pytest.fixture
def zernio(monkeypatch):
    monkeypatch.setenv("ZERNIO_API_KEY", "test-key")
    monkeypatch.setenv("ZERNIO_ACCOUNT_ID", "acc1")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    get = MagicMock()
    monkeypatch.setattr(aa.requests, "get", get)
    return get


# ── запросы к Zernio ─────────────────────────────────────────────────────────

def test_headers_require_key(monkeypatch):
    monkeypatch.setenv("ZERNIO_API_KEY", "")
    with pytest.raises(EnvironmentError):
        aa._headers()


@pytest.mark.parametrize("payload,expected", [
    ([{"_id": "a"}], [{"_id": "a"}]),
    ({"posts": [{"_id": "b"}]}, [{"_id": "b"}]),
    ({"data": [{"_id": "c"}]}, [{"_id": "c"}]),
    ({"other": 1}, []),
])
def test_fetch_posts_accepts_all_shapes(zernio, payload, expected):
    zernio.return_value = _resp(payload)
    assert aa._fetch_posts("acc1", limit=10) == expected
    assert zernio.call_args.args[0].endswith("/posts")
    assert zernio.call_args.kwargs["params"] == {"accountId": "acc1", "limit": 10}
    assert zernio.call_args.kwargs["headers"]["Authorization"] == "Bearer test-key"


def test_fetch_post_analytics_404_is_empty(zernio):
    zernio.return_value = _resp({"error": "not found"}, status=404)
    assert aa._fetch_post_analytics("p1") == {}


def test_fetch_post_analytics_5xx_raises(zernio):
    zernio.return_value = _resp({}, status=500)
    with pytest.raises(requests.HTTPError):
        aa._fetch_post_analytics("p1")


# ── YouTube Data API ─────────────────────────────────────────────────────────

def test_youtube_stats_empty_ids_skip_request(zernio, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "yt-key")
    assert aa._fetch_youtube_stats([]) == {}
    assert not zernio.called


def test_youtube_stats_batches_by_50_and_survives_failed_batch(zernio, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "yt-key")
    ids = [f"v{i}" for i in range(120)]
    zernio.side_effect = [
        _resp({"items": [{"id": "v0", "statistics": {"viewCount": "7", "likeCount": None}}]}),
        requests.ConnectionError("down"),
        _resp({"items": [{"id": "v100", "statistics": {}}]}),
    ]
    stats = aa._fetch_youtube_stats(ids)
    assert stats == {"v0": {"views": 7, "likes": 0, "comments": 0},
                     "v100": {"views": 0, "likes": 0, "comments": 0}}
    batches = [c.kwargs["params"]["id"].count(",") + 1 for c in zernio.call_args_list]
    assert batches == [50, 50, 20]
    assert zernio.call_args.kwargs["headers"] == {"X-Goog-Api-Key": "yt-key"}   # ключ не в URL


# ── нормализация ─────────────────────────────────────────────────────────────

def test_parse_post_platform_as_string_and_title_fallback():
    post = {"id": "p1", "platforms": ["tiktok"], "content": "я" * 100, "createdAt": "2026-10-20T10:00:00Z"}
    row = aa._parse_post(post, _synced(5))
    assert row["platform"] == "tiktok" and row["title"] == "я" * 80
    assert row["published_at"] == "2026-10-20T10:00:00Z" and row["views"] == 5


def test_parse_post_platform_field_without_list():
    row = aa._parse_post({"_id": "p1", "platform": "instagram", "title": "T"}, {})
    assert row["platform"] == "instagram" and row["metrics_source"] is None and row["fetched_at"] is None


def test_zernio_metrics_last_updated_counts_as_synced():
    assert aa._zernio_metrics({"analytics": {"views": "3", "lastUpdated": "2026-10-20"}}) == \
        {"views": 3, "likes": 0, "comments": 0}


def test_first_platform_ignores_non_dict():
    assert aa._first_platform({"platforms": ["youtube"]}) == {}
    assert aa._platform_post_id({"platforms": [{"platform": "youtube"}]}) == ""


# ── run ──────────────────────────────────────────────────────────────────────

def test_run_requires_account(monkeypatch, tmp_path):
    monkeypatch.setenv("ZERNIO_ACCOUNT_ID", "")
    with pytest.raises(EnvironmentError, match="ZERNIO_ACCOUNT_ID"):
        aa.run(db_path=str(tmp_path / "p.db"))


def test_run_reraises_posts_http_error(zernio, tmp_path):
    zernio.return_value = _resp({}, status=401)
    with pytest.raises(requests.HTTPError):
        aa.run(db_path=str(tmp_path / "p.db"))


def test_run_window_skips_old_and_idless_posts_and_returns_top3(zernio, tmp_path):
    posts = [
        {"_id": "old", "title": "старый", "publishedAt": _iso(30), "platforms": [{"platform": "tiktok"}]},
        {"title": "без id", "publishedAt": _iso(1)},
        {"_id": "a", "title": "A", "publishedAt": _iso(1), "platforms": [{"platform": "tiktok"}]},
        {"_id": "b", "title": "B", "publishedAt": _iso(2), "platforms": [{"platform": "instagram"}]},
        {"_id": "c", "title": "C", "publishedAt": _iso(3), "platforms": [{"platform": "tiktok"}]},
        {"_id": "d", "title": "D", "publishedAt": _iso(4), "platforms": [{"platform": "tiktok"}]},
        {"_id": "e", "title": "E", "publishedAt": _iso(5), "platforms": [{"platform": "tiktok"}]},
    ]
    analytics = {"a": _resp(_synced(10)), "b": _resp(_synced(300, 4, 1)), "c": _resp({"syncStatus": "pending"}),
                 "d": _resp(_synced(50))}

    def get(url, **kw):
        if url.endswith("/posts"):
            return _resp({"posts": posts})
        pid = kw["params"]["postId"]
        if pid == "e":
            raise requests.ConnectionError("down")                 # одна недоступная аналитика не валит сбор
        return analytics[pid]

    zernio.side_effect = get
    top = aa.run(days=7, db_path=str(tmp_path / "p.db"))
    assert [t["post_id"] for t in top] == ["b", "d", "a"]
    assert top[0] == {"post_id": "b", "platform": "instagram", "title": "B", "views": 300, "likes": 4,
                      "comments": 1, "published_at": posts[3]["publishedAt"]}
    asked = [c.kwargs["params"]["postId"] for c in zernio.call_args_list if c.args[0].endswith("/analytics")]
    assert sorted(asked) == ["a", "b", "c", "d", "e"]               # старый пост и пост без id не запрашиваются


def test_run_youtube_with_stats_skips_zernio_analytics(zernio, tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "yt-key")
    post = {"_id": "p1", "title": "Кабинет 7 · 1", "publishedAt": _iso(1),
            "platforms": [{"platform": "youtube", "platformPostId": "vid1"}]}

    def get(url, **kw):
        if url.endswith("/posts"):
            return _resp([post])
        if url == aa.YOUTUBE_VIDEOS_URL:
            return _resp({"items": [{"id": "vid1", "statistics": {"viewCount": "42"}}]})
        raise AssertionError(f"лишний запрос {url}")

    zernio.side_effect = get
    top = aa.run(db_path=str(tmp_path / "p.db"))
    assert top[0]["views"] == 42


def test_ensure_table_migrates_old_schema(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "p.db")
    conn.execute("CREATE TABLE performance (id INTEGER PRIMARY KEY, post_id TEXT, platform TEXT, title TEXT, "
                 "published_at TEXT)")
    aa._ensure_table(conn)
    aa._ensure_table(conn)                                          # повторный вызов безопасен
    cols = {r[1] for r in conn.execute("PRAGMA table_info(performance)")}
    assert {"views", "likes", "comments", "fetched_at", "niche", "hook"} <= cols
    conn.close()

"""
Регрессионные тесты на исправления аудита 2026-10-18:
маскирование секретов, fail-closed гейты, идемпотентная публикация,
защита админ-API.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent


def _resp(content: str) -> MagicMock:
    return MagicMock(choices=[MagicMock(message=MagicMock(content=content))])


# ── utils.log_redact ─────────────────────────────────────────────────────────

def test_redact_masks_telegram_token_google_key_and_env_secret():
    from utils.log_redact import redact

    text = (
        "400 Client Error for url: https://api.telegram.org/bot1234567890:AAHabcdefghijklmnopqrstuvwxyz012345/sendMessage "
        "and https://www.googleapis.com/youtube/v3/videos?part=snippet&key=AIzaSyA1234567890abcdefghijklmnopqrstu "
        "and secret-value-xyz-123"
    )
    with patch.dict("os.environ", {"SOME_SERVICE_API_KEY": "secret-value-xyz-123"}):
        out = redact(text)
    assert "AAHabcdef" not in out
    assert "AIzaSyA123" not in out
    assert "secret-value-xyz-123" not in out
    assert "sendMessage" in out


def test_redacting_filter_cleans_log_records():
    import logging
    from utils.log_redact import RedactingFilter

    record = logging.LogRecord("x", logging.ERROR, __file__, 1,
                               "fail: %s", ("https://api.telegram.org/bot123456789:AA" + "x" * 33 + "/send",), None)
    RedactingFilter().filter(record)
    assert "xxxxxxxx" not in record.getMessage()


# ── utils.providers ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("402 Client Error: Payment Required", True),
    ("{'code': 402, 'msg': 'Credits insufficient'}", True),
    ("task 4021 finished in 402ms", False),
    ("video id ab402cd", False),
    ("quota exceeded, limit: 0", True),
])
def test_looks_like_no_credits(text, expected):
    from utils.providers import looks_like_no_credits
    assert looks_like_no_credits(text) is expected


# ── safety_agent: fail-closed ────────────────────────────────────────────────

def test_safety_agent_all_engines_down_is_unsafe():
    from agents import safety_agent
    with patch("utils.llm_client.chat", side_effect=RuntimeError("all providers down")):
        result = safety_agent.check("текст", "заголовок", [], "tiktok")
    assert result["safe"] is False
    assert result["category"] == "unchecked"


def test_safety_agent_string_false_is_unsafe():
    from agents.safety_agent import _parse_result
    assert _parse_result('{"safe": "false", "issue": "x", "category": "nsfw"}')["safe"] is False


def test_safety_agent_missing_safe_field_raises():
    from agents.safety_agent import _parse_result
    with pytest.raises(ValueError):
        _parse_result('{"issue": null}')


# ── topic_filter: fail-closed ────────────────────────────────────────────────

_IDEAS = [
    {"title": "Идея A", "hook": "a", "source_topic": "a", "virality": 0.5},
    {"title": "Идея B", "hook": "b", "source_topic": "b", "virality": 0.4},
]


def test_topic_filter_idea_without_verdict_is_dropped():
    from agents import topic_filter
    with patch("utils.llm_client.chat", return_value=_resp('{"results": [{"id": 1, "safe": true}]}')) as llm:
        result = topic_filter.run(_IDEAS, platform="tiktok", db_path=":memory:")
    assert llm.call_count == 2  # неполный ответ → переспросили один раз
    assert [i["title"] for i in result] == ["Идея A"]


def test_topic_filter_all_engines_down_raises():
    from agents import topic_filter
    with patch("utils.llm_client.chat", side_effect=RuntimeError("down")):
        with pytest.raises(RuntimeError):
            topic_filter.run(_IDEAS, platform="tiktok", db_path=":memory:")


# ── fact_checker: fail-closed ────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["не JSON вообще", '{"valid": "maybe"}', '{"errors": []}'])
def test_fact_checker_unparseable_is_invalid(raw):
    from agents.fact_checker import check
    with patch.dict("os.environ", {"GROQ_API_KEY": "k"}), \
         patch("utils.llm_client.groq_with_cerebras_fallback", return_value=_resp(raw)):
        result = check("текст")
    assert result["valid"] is False


def test_fact_checker_passes_facts_to_prompt():
    from agents.fact_checker import check
    with patch.dict("os.environ", {"GROQ_API_KEY": "k"}), \
         patch("utils.llm_client.groq_with_cerebras_fallback",
               return_value=_resp('{"valid": true}')) as mock_llm:
        result = check("текст", facts=[{"text": "Факт про 23 минуты", "source": "UC Irvine"}])
    assert result == {"valid": True}
    user_msg = mock_llm.call_args.kwargs["messages"][1]["content"]
    assert "Факт про 23 минуты" in user_msg


# ── quality_scorer: без фиктивных оценок ─────────────────────────────────────

def test_quality_scorer_error_is_skipped_not_fake_score(tmp_path):
    from agents import quality_scorer
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0" * 100)
    with patch.dict("os.environ", {"GEMINI_API_KEY": "k"}), \
         patch.object(quality_scorer, "_extract_frames", return_value=["a.jpg", "b.jpg"]), \
         patch.object(quality_scorer, "_vision_score", side_effect=RuntimeError("429")):
        result = quality_scorer.score_video(str(video))
    assert result["verdict"] == "skipped"
    assert result["score"] is None


# ── publisher_agent: без дублей ──────────────────────────────────────────────

def _ok_response(payload: dict) -> MagicMock:
    r = MagicMock(status_code=200, ok=True)
    r.json.return_value = payload
    r.raise_for_status = MagicMock()
    return r


def test_publisher_does_not_retry_create_post(tmp_path):
    from agents import publisher_agent
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0" * 1024)
    presign = _ok_response({"uploadUrl": "https://u", "publicUrl": "https://p"})
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        if url.endswith("/media/presign"):
            return presign
        raise requests.ConnectionError("connection reset")

    with patch.dict("os.environ", {"ZERNIO_API_KEY": "k"}), \
         patch.object(publisher_agent.requests, "post", side_effect=fake_post), \
         patch.object(publisher_agent.requests, "put", return_value=_ok_response({})):
        with pytest.raises(requests.ConnectionError):
            publisher_agent.publish_video(str(video), "t", "d", [], "youtube", "acc")
    assert sum(1 for u in calls if u.endswith("/posts")) == 1


def test_publisher_requires_post_id(tmp_path):
    from agents import publisher_agent
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0" * 1024)
    resp = _ok_response({"uploadUrl": "https://u", "publicUrl": "https://p", "post": {"status": "published"}})
    with patch.dict("os.environ", {"ZERNIO_API_KEY": "k"}), \
         patch.object(publisher_agent.requests, "post", return_value=resp), \
         patch.object(publisher_agent.requests, "put", return_value=_ok_response({})):
        with pytest.raises(RuntimeError, match="id поста"):
            publisher_agent.publish_video(str(video), "t", "d", [], "youtube", "acc")


def test_publisher_requires_account_id(tmp_path):
    from agents import publisher_agent
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0")
    with pytest.raises(ValueError, match="account_id"):
        publisher_agent.publish_video(str(video), "t", "d", [], "youtube", "")


# ── utils.api_server: защита от CSRF / DNS rebinding ─────────────────────────

def _api_check(script: str, tmp_path: Path) -> dict:
    """api_server подменяет sys.stdout при импорте — гоняем в отдельном процессе."""
    result_file = tmp_path / "result.json"
    code = textwrap.dedent("""
        import json, sys
        from unittest.mock import patch, MagicMock
        sys.path.insert(0, %r)
        import utils.api_server as api
        client = api.app.test_client()
        out = {}
    """ % str(ROOT)) + textwrap.dedent(script) + (
        "\nopen(%r, 'w', encoding='utf-8').write(json.dumps(out, ensure_ascii=False))\n" % str(result_file)
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          cwd=str(ROOT), timeout=120)
    assert result_file.exists(), proc.stderr.decode("utf-8", "replace")[-2000:]
    return json.loads(result_file.read_text(encoding="utf-8"))


def test_api_rejects_cross_origin_and_non_json_and_foreign_host(tmp_path):
    out = _api_check("""
        r = client.post("/api/run", data='{"niche": "x"}', content_type="text/plain")
        out["text_plain"] = r.status_code
        r = client.post("/api/run", json={"niche": "x"}, headers={"Origin": "https://evil.example"})
        out["cross_origin"] = r.status_code
        r = client.get("/api/stats", headers={"Host": "evil.example"})
        out["foreign_host"] = r.status_code
    """, tmp_path)
    assert out == {"text_plain": 415, "cross_origin": 403, "foreign_host": 403}


def test_api_jobs_build_commands_and_require_publish_confirm(tmp_path):
    out = _api_check("""
        api.JOBS_DIR = __import__("pathlib").Path(%r)
        with patch.object(api.subprocess, "Popen") as popen, patch.object(api.threading, "Thread"):
            popen.return_value.pid = 1
            r = client.post("/api/jobs", json={"type": "prepare", "series": "kabinet7", "n": 2})
            out["prepare"] = r.status_code
            out["cmd"] = popen.call_args.args[0][3:]
            out["no_confirm"] = client.post("/api/jobs", json={"type": "publish_next", "series": "kabinet7"}).status_code
            out["bad_series"] = client.post("/api/jobs", json={"type": "prepare", "series": "../x"}).status_code
            out["bad_script"] = client.post("/api/jobs", json={"type": "script", "series": "kabinet7",
                                                               "file": "../../.env"}).status_code
    """ % str(tmp_path / "jobs"), tmp_path)
    assert out["prepare"] == 201
    assert out["cmd"][0].endswith("main.py") and out["cmd"][1:] == [
        "--format", "series", "--series", "kabinet7", "--niche", "психология", "--platform", "youtube", "--prepare", "2"]
    assert out["no_confirm"] == 400 and out["bad_series"] == 400 and out["bad_script"] == 400


# ── main.run_pipeline: dry-run, fail-closed fact-check, одна попытка публикации ─

_PIPELINE_HARNESS = """
import contextlib, json, os, sys
from unittest.mock import patch, MagicMock
os.environ["DB_PATH"] = %(db)r
sys.path.insert(0, %(root)r)
sys.argv = ["main.py"]
for _k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ZERNIO_API_KEY"):
    os.environ[_k] = ""  # до load_dotenv в main: настоящие ключи из .env не подхватятся (см. conftest.py)
import main
out = {}
idea = {"title": "Тема", "hook": "h", "hashtags": [], "source_topic": "тема", "virality": 1}
script = {"full_script": "слово " * 130, "hook": "h", "body": "b", "min_words": 120}

def run(dry_run, fact=None, publisher=None):
    publisher = publisher or MagicMock(return_value={"post_id": "p1", "status": "published", "platform": "youtube"})
    mark, notify, alert = MagicMock(), MagicMock(), MagicMock()
    patches = [
        patch.object(main, "step_trend_scout", return_value=[{"topic": "тема"}]),
        patch.object(main, "step_idea_generator", return_value=[idea]),
        patch.object(main, "step_topic_filter", return_value=[idea]),
        patch.object(main, "step_research_agent", return_value={"facts": [], "summary": "s"}),
        patch.object(main, "step_script_writer", return_value=dict(script)),
        patch.object(main, "step_fact_checker", return_value=fact or {"valid": True}),
        patch("agents.script_writer.improve_hook", return_value={"chosen_index": 0}),
        patch("agents.script_writer.fix_script", return_value=None),  # без живого LLM
        patch.object(main, "step_series_check", return_value=None),
        patch.object(main, "step_safety_agent", return_value={"safe": True}),
        patch.object(main, "step_voice_agent", return_value={"audio_path": "a.mp3"}),
        patch.object(main, "step_visual_agent", return_value={"clips": ["c.mp4"]}),
        patch.object(main, "step_music_agent", return_value=None),
        patch("agents.assembly_agent._get_duration", return_value=45.0),
        patch.object(main, "step_subtitle_agent", return_value=None),
        patch.object(main, "step_get_photos", return_value=[]),
        patch.object(main, "step_entity_visuals", return_value={"overlays": [], "found": 0}),
        patch.object(main, "step_counter_overlays", return_value=[]),
        patch.object(main, "step_assembly_agent", return_value={"video_path": "v.mp4", "duration": 45}),
        patch.object(main, "step_thumbnail_agent", return_value=None),
        patch.object(main, "_quality_gate"),
        patch.object(main, "step_qa_agent", return_value={"status": "approved"}),
        patch.object(main, "step_publisher_agent", publisher),
        patch.object(main, "step_mark_published", mark),
        patch.object(main, "step_notify_success", notify),
        patch.object(main, "_alert", alert),
        patch("agents.comment_writer.write_pinned_comment", return_value="c"),
        patch.object(main.time, "sleep"),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        code = 0
        try:
            main.run_pipeline("ниша", "youtube", dry_run=dry_run)
        except SystemExit as exc:
            code = exc.code
    return {"code": code, "mark": mark.called, "publisher_calls": publisher.call_count,
            "notify_dry": notify.call_args.kwargs.get("dry_run") if notify.called else None,
            "alert": alert.called}

out["dry"] = run(True)
out["real"] = run(False)
out["fact_fail_real"] = run(False, fact={"valid": False, "errors": ["x"]})
out["fact_fail_dry"] = run(True, fact={"valid": False, "errors": ["x"]})
out["publish_error"] = run(False, publisher=MagicMock(side_effect=ConnectionError("reset")))
open(%(result)r, "w", encoding="utf-8").write(json.dumps(out))
"""


def test_run_pipeline_dry_run_fact_check_and_single_publish(tmp_path):
    result_file = tmp_path / "result.json"
    code = _PIPELINE_HARNESS % {"db": str(tmp_path / "p.db"), "root": str(ROOT), "result": str(result_file)}
    # cwd=tmp_path: main.py пишет logs/ относительно cwd — тест не должен мусорить в проекте
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, cwd=str(tmp_path), timeout=300)
    assert result_file.exists(), proc.stderr.decode("utf-8", "replace")[-3000:]
    out = json.loads(result_file.read_text(encoding="utf-8"))

    # dry-run: тема не помечается, уведомление помечено как dry-run
    assert out["dry"] == {"code": 0, "mark": False, "publisher_calls": 1, "notify_dry": True, "alert": False}
    # реальный режим: тема помечается после публикации
    assert out["real"]["code"] == 0 and out["real"]["mark"] is True
    # fact-check не пройден: в реальном режиме — отмена и алерт, публикации нет
    assert out["fact_fail_real"]["code"] == 1
    assert out["fact_fail_real"]["publisher_calls"] == 0 and out["fact_fail_real"]["alert"] is True
    # ... а dry-run собирает видео для просмотра
    assert out["fact_fail_dry"]["code"] == 0
    # ошибка публикации — ровно одна попытка
    assert out["publish_error"]["code"] == 1 and out["publish_error"]["publisher_calls"] == 1

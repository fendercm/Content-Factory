"""
Quick-test suite для Admin Panel (/api/test/<agent>).
Каждая check_*-функция возвращает {"success": bool, "message": str}.

Функции названы check_*, а не test_*: раньше pytest собирал их как тесты,
а они ловят все исключения и возвращают dict — поэтому всегда были «зелёными»,
даже когда 5 из 12 падали. Теперь pytest гоняет их через test_quick_agent
и проверяет success.

Запуск из командной строки:
    py tests/test_agents.py              # все тесты
    py tests/test_agents.py fact_checker # один агент
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = str(Path(__file__).parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── Mock helpers ──────────────────────────────────────────────────────────────

def _groq_mock(payload: dict | list) -> MagicMock:
    content = json.dumps(payload, ensure_ascii=False)
    resp = MagicMock()
    resp.choices[0].message.content = content
    client = MagicMock()
    client.chat.completions.create.return_value = resp
    return client


def _llm_resp_mock(payload: dict | list | str) -> MagicMock:
    """Mock response returned by groq_with_cerebras_fallback (not a client, just the response)."""
    content = json.dumps(payload, ensure_ascii=False) if isinstance(payload, (dict, list)) else payload
    resp = MagicMock()
    resp.choices[0].message.content = content
    return resp


# ── Individual tests ──────────────────────────────────────────────────────────

def check_trend_scout() -> dict:
    try:
        with patch("agents.trend_scout.TrendReq") as mock_tr, \
             patch("agents.trend_scout.requests.get") as mock_req, \
             patch("feedparser.parse", return_value=MagicMock(entries=[])):
            mock_df = MagicMock()
            mock_df.empty = True
            mock_tr.return_value.build_payload = MagicMock()
            mock_tr.return_value.related_queries.return_value = {}
            mock_req.return_value.status_code = 200
            mock_req.return_value.raise_for_status = MagicMock()
            mock_req.return_value.json.return_value = {"items": []}
            from agents.trend_scout import run
            result = run("психология")
        return {"success": isinstance(result, list), "message": f"Вернул {len(result)} тем"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def check_idea_generator() -> dict:
    try:
        from agents.idea_generator import run
        payload = [
            {"title": "5 ловушек мозга", "hook": "Ты когда-нибудь...",
             "angle": "Когнитивные ловушки влияют на каждого", "hashtags": ["психология"],
             "source_topic": "психология мышления", "virality": 0.85},
        ]
        topics = [{"topic": "психология мышления", "virality": 0.85, "source": "google_trends"}]
        with patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}), \
             patch("utils.llm_client.groq_with_cerebras_fallback",
                   return_value=_llm_resp_mock(payload)):
            result = run(topics)
        ok = isinstance(result, list) and len(result) > 0 and "title" in result[0]
        title = result[0]["title"] if result else "—"
        return {"success": ok, "message": f"Вернул {len(result)} идей: «{title}»"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def check_topic_filter() -> dict:
    try:
        from agents.topic_filter import run
        ideas = [{"title": "5 ловушек мозга", "hook": "...", "angle": "...",
                  "hashtags": ["психология"], "source_topic": "психология", "virality": 0.85}]
        with patch("agents.topic_filter.GEMINI_API_KEY", "test-key"), \
             patch("utils.providers.is_enabled", return_value=True), \
             patch("agents.topic_filter.genai") as mock_genai:
            mock_genai.Client.return_value.models.generate_content.return_value = \
                MagicMock(text='[{"id": 1, "safe": true, "reason": ""}]')
            result = run(ideas, platform="tiktok", db_path=":memory:")
        return {"success": len(result) == 1, "message": f"{len(result)} идей прошли фильтр"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def check_script_writer() -> dict:
    try:
        from agents.script_writer import write_script
        json_payload = {
            "hook": "Ты знаешь, что 90% решений принимается неосознанно?",
            "body": "Мозг обрабатывает 11 млн бит в секунду...",
            "cta": "Напиши в комментарии какую ловушку ты замечал",
        }
        # Step 1 gets called up to 3x expecting plain text; we return JSON which fails word
        # count (too short) — the loop exhausts and uses last raw_text as full_script.
        # Step 2 also gets the same JSON, parses it successfully → all required keys present.
        with patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}), \
             patch("utils.llm_client.groq_with_cerebras_fallback",
                   return_value=_llm_resp_mock(json_payload)):
            result = write_script("5 ловушек мозга", "tiktok")
        ok = {"hook", "body", "cta", "full_script"} <= result.keys()
        return {"success": ok, "message": f"hook: «{result['hook'][:50]}»"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def check_fact_checker() -> dict:
    try:
        from agents.fact_checker import check
        bad_text = "Алексей Немов — легендарный российский футболист, завоевавший 6 медалей."
        good_text = "Регулярные тренировки улучшают когнитивные функции на 20%."
        with patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}), \
             patch("utils.llm_client.groq_with_cerebras_fallback",
                   return_value=_llm_resp_mock({"valid": False, "errors": ["Немов — гимнаст, не футболист"]})):
            bad_result = check(bad_text)
        with patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}), \
             patch("utils.llm_client.groq_with_cerebras_fallback",
                   return_value=_llm_resp_mock({"valid": True})):
            good_result = check(good_text)
        ok = (bad_result["valid"] is False) and (good_result["valid"] is True)
        return {"success": ok, "message": "valid=False для ошибочного текста ✓, valid=True для верного ✓"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def check_safety_agent() -> dict:
    try:
        from agents.safety_agent import check
        with patch("agents.safety_agent.GEMINI_API_KEY", "test-key"), \
             patch("utils.providers.is_enabled", return_value=True), \
             patch("google.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.return_value = \
                MagicMock(text='{"safe": true}')
            result = check(
                script_text="Привычки успешных людей помогают достигать целей",
                title="5 привычек", hashtags=["психология"], platform="tiktok"
            )
        return {"success": result.get("safe", False), "message": "Контент безопасен ✓"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def check_voice_agent() -> dict:
    try:
        from agents.voice_agent import generate_voice
        async def fake_save(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"\xff\xfb\x00" * 50)

        mock_instance = MagicMock()
        mock_instance.save = fake_save

        with patch("edge_tts.Communicate", return_value=mock_instance), \
             patch("agents.voice_agent.KIE_API_KEY", ""), \
             patch("agents.voice_agent.DASHSCOPE_API_KEY", ""), \
             patch.dict("os.environ", {"YANDEX_API_KEY": ""}), \
             patch("agents.voice_agent._normalize_volume"):
            result = generate_voice("Тестовый текст для синтеза голоса.", "tiktok")
        ok = "audio_path" in result
        if ok and os.path.exists(result["audio_path"]):
            os.remove(result["audio_path"])
        return {"success": ok, "message": "audio_path создан ✓"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def check_music_agent() -> dict:
    try:
        from agents.music_agent import get_music
        with patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}), \
             patch("utils.llm_client.groq_with_cerebras_fallback",
                   return_value=_llm_resp_mock("upbeat")), \
             patch("agents.music_agent.os.path.isdir", return_value=True), \
             patch("agents.music_agent.os.listdir", return_value=["mock.mp3"]), \
             patch("agents.music_agent.os.path.exists", return_value=True):
            result = get_music("Привычки успешных людей помогают достигать целей.")
        ok = "mood" in result
        return {"success": ok, "message": f"mood={result.get('mood','?')}, source={result.get('source','?')}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def check_visual_agent() -> dict:
    try:
        from agents.visual_agent import get_visuals
        fake_video = {
            "id": 99,
            "duration": 10,
            "video_files": [{"width": 1080, "height": 1920, "link": "http://example.com/v.mp4"}],
        }
        content_iter = iter([b"\x00" * 256])

        class _FakeResp:
            status_code = 200
            def json(self): return {"videos": [fake_video]}
            def raise_for_status(self): pass
            def iter_content(self, **kw): return content_iter
            def __enter__(self): return self
            def __exit__(self, *a): pass

        with patch.dict("os.environ", {"PEXELS_API_KEY": "test-key", "GROQ_API_KEY": "test-key"}), \
             patch("agents.visual_agent.requests.get", return_value=_FakeResp()), \
             patch("agents.visual_agent._niche_override", return_value="psychology mind"), \
             patch("agents.visual_agent._download_clip"):
            result = get_visuals("психология мышления", "tiktok", num_clips=1)
        ok = "clips" in result
        return {"success": ok, "message": f"query='{result.get('query','?')}' ✓"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def check_qa_agent() -> dict:
    try:
        from agents.qa_agent import check
        fake_probe = json.dumps({
            "streams": [
                {"codec_type": "video", "width": 1080, "height": 1920, "r_frame_rate": "30/1"},
                {"codec_type": "audio"},
            ],
            "format": {"duration": "45.0", "size": "5000000"},
        })
        with patch("agents.qa_agent.subprocess.run") as mock_run, \
             patch("agents.qa_agent.shutil.which", return_value="/usr/bin/ffprobe"), \
             patch("agents.qa_agent.os.path.exists", return_value=True), \
             patch("agents.qa_agent.os.path.getsize", return_value=5_000_000):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = fake_probe
            result = check("/fake/video.mp4", "tiktok", expected_duration=45)
        ok = result.get("status") == "approved"
        return {"success": ok, "message": f"status={result.get('status')} ✓"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def check_publisher_agent() -> dict:
    try:
        from agents.publisher_agent import publish_video
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "uploadUrl": "https://upload.example/put", "publicUrl": "https://cdn.example/v.mp4",
            "post": {"_id": "mock-123", "status": "published"},
        }
        mock_resp.raise_for_status = MagicMock()
        with patch.dict("os.environ", {"ZERNIO_API_KEY": "test-key"}), \
             patch("agents.publisher_agent.requests.post", return_value=mock_resp), \
             patch("agents.publisher_agent.requests.put", return_value=mock_resp), \
             patch("agents.publisher_agent.os.path.exists", return_value=True), \
             patch("agents.publisher_agent.os.path.getsize", return_value=1024), \
             patch("builtins.open", MagicMock()):
            result = publish_video(
                video_path="/fake/video.mp4", title="Тест",
                description="Тест", hashtags=["тест"],
                platform="tiktok", account_id="acc-1",
            )
        ok = result.get("post_id") == "mock-123"
        return {"success": ok, "message": f"post_id={result.get('post_id','?')} ✓"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def check_notify_agent() -> dict:
    try:
        from agents.notify_agent import send
        with patch("agents.notify_agent.TELEGRAM_BOT_TOKEN", "123:ABC"), \
             patch("agents.notify_agent.TELEGRAM_CHAT_ID", "456"), \
             patch("agents.notify_agent.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"ok": True, "result": {"message_id": 1}}
            res = send("Тест уведомления", level="info")
        return {"success": res.get("ok") is True, "message": "Telegram mock-отправка ✓"}
    except Exception as e:
        return {"success": False, "message": str(e)}


# ── Registry ──────────────────────────────────────────────────────────────────

AGENT_TESTS: dict[str, tuple] = {
    "trend_scout":    (check_trend_scout,    "Google Trends + YouTube + RSS"),
    "idea_generator": (check_idea_generator, "Генерация идей через Groq"),
    "topic_filter":   (check_topic_filter,   "Дедупликация + Gemini safety"),
    "script_writer":  (check_script_writer,  "Сценарий PAS через Groq"),
    "fact_checker":   (check_fact_checker,   "valid=False для текста с ошибкой"),
    "safety_agent":   (check_safety_agent,   "Проверка политик платформ"),
    "voice_agent":    (check_voice_agent,    "Синтез голоса edge-tts"),
    "music_agent":    (check_music_agent,    "Подбор фоновой музыки по настроению"),
    "visual_agent":   (check_visual_agent,   "Скачивание клипов с Pexels"),
    "qa_agent":       (check_qa_agent,       "QA проверка через ffprobe"),
    "publisher_agent":(check_publisher_agent,"Публикация через Zernio API"),
    "notify_agent":   (check_notify_agent,   "Telegram уведомления"),
}


def run_agent_test(agent_name: str) -> dict:
    """Вызывается из api_server.py → POST /api/test/<agent>."""
    if agent_name not in AGENT_TESTS:
        return {"success": False, "message": f"Агент '{agent_name}' не найден в реестре"}
    fn, _ = AGENT_TESTS[agent_name]
    return fn()


# ── pytest ────────────────────────────────────────────────────────────────────

def test_quick_agent_checks():
    """Каждая check_*-функция должна вернуть success=True."""
    failed = {}
    for name, (fn, _) in AGENT_TESTS.items():
        res = fn()
        if not res["success"]:
            failed[name] = res["message"]
    assert not failed, failed


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else None
    if name:
        res = run_agent_test(name)
        icon = "✅" if res["success"] else "❌"
        print(f"{icon} {name}: {res['message']}")
    else:
        print("Запускаю все тесты...\n")
        for agent, (fn, desc) in AGENT_TESTS.items():
            t0 = time.time()
            res = fn()
            ms = round((time.time() - t0) * 1000)
            icon = "✅" if res["success"] else "❌"
            print(f"{icon} {agent:20s} ({ms:4d}ms)  {res['message']}")

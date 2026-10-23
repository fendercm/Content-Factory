"""Лаборатория v2: завязки с отметками автора → история → диалог по тактам → критик и переписка."""

import json
from pathlib import Path

import pytest

from agents import anthology_agent as aa
from tests.test_audit_fixes import _api_check

ROOT = Path(__file__).resolve().parent.parent
BUBLIK = json.loads((ROOT / "config" / "series" / "scripts" / "kabinet7_bublik.json").read_text(encoding="utf-8"))
PITCH = {"title": "Бублик", "logline": "Артём врёт маме про папину собаку.", "object": "фото собаки",
         "now": "мама прилетела", "twist": "мама давно знает", "ending": "зовёт маму",
         "client_name": "Артём", "age": 32, "gender": "male", "occupation": "инженер"}


def _screenplay(lines: list[dict], title: str = "Бублик") -> str:
    return f"НАЗВАНИЕ: {title}\nСЦЕНАРИЙ:\n" + "\n".join(
        f"{l['role']}: [{l.get('emotion', 'тихо')}] {l['text']}" for l in lines)


@pytest.fixture
def series():
    return aa.load_series("kabinet7")


def test_pitches_votes_and_taste(tmp_path, series):
    db = str(tmp_path / "p.db")
    ids = aa.save_pitches(series, [PITCH, {**PITCH, "title": "Другая", "logline": "Скучная история про офис."}],
                          "20261021_1800", db)
    assert aa._taste_text(series["id"], db) == ""
    assert aa.set_pitch(ids[0], db, vote=1) and aa.set_pitch(ids[1], db, vote=-1)
    assert not aa.set_pitch(999, db, vote=1)
    taste = aa._taste_text(series["id"], db)
    assert "+ Артём врёт маме" in taste and "- Скучная история" in taste
    assert [p["title"] for p in aa.list_pitches(series["id"], db, ids=[ids[0]])] == ["Бублик"]


def test_write_from_pitch_critic_edits_text_itself(monkeypatch, series):
    weak = [dict(l) for l in BUBLIK["lines"]]
    weak[-1] = {**weak[-1], "text": "Наверное, мне стоит подумать."}
    calls, prompts = [], []
    critic = iter([
        {"checks": {"ending_action": False}, "watch": 5, "fixes": ["реплика 13: финал — вывод, нужен поступок"],
         "script": _screenplay(BUBLIK["lines"])},
        {"checks": {k: True for k in aa._CRITIC_CHECKS}, "watch": 8, "fixes": [], "script": None},
    ])

    def fake_llm(messages, tier, json_mode, max_tokens, caller):
        calls.append(caller)
        if caller == "anthology_story":
            return json.dumps({"story": "Артём два года шлёт маме фото собаки...", **BUBLIK["case"]},
                              ensure_ascii=False)
        if caller == "anthology_critic":
            prompts.append(messages[-1]["content"])
            return json.dumps(next(critic), ensure_ascii=False)
        return _screenplay(weak)

    monkeypatch.setattr(aa, "_llm", fake_llm)
    ep = aa.write_from_pitch(series, PITCH)
    assert calls == ["anthology_story", "anthology_episode", "anthology_critic", "anthology_critic"]  # сценарист не переписывает
    assert '"script"' in prompts[0]
    assert ep["lines"][-1]["text"] == "Позовите её. Пожалуйста."                  # правка критика в тексте
    assert ep["critic"]["score"] == 8 and [r["score"] for r in ep["critic"]["rounds"]] == [5, 8]
    assert ep["story"].startswith("Артём") and ep["case"]["signature_detail"]


def test_long_draft_is_not_rejected_and_broken_edit_keeps_best(monkeypatch, series):
    long_lines = [{**l, "text": l["text"] + " " + " ".join(f"доп{i}x{k}" for k in range(6))}
                  for i, l in enumerate(BUBLIK["lines"])]   # ~190 слов
    critic = iter([{"checks": {"reveals": False}, "watch": 6, "fixes": ["сократить"], "script": "не сценарий"}])

    def fake_llm(messages, tier, json_mode, max_tokens, caller):
        if caller == "anthology_story":
            return json.dumps({"story": "история", **BUBLIK["case"]}, ensure_ascii=False)
        if caller == "anthology_critic":
            return json.dumps(next(critic), ensure_ascii=False)
        return _screenplay(long_lines)

    monkeypatch.setattr(aa, "_llm", fake_llm)
    ep = aa.write_from_pitch(series, PITCH)
    assert ep["critic"]["score"] == 6 and ep["critic"]["rounds"][0]["words"] > series["words_max"] + 10
    assert ep["lines"][0]["text"].endswith("доп0x5")    # испорченная правка не заменила сценарий


def test_write_from_pitch_keeps_best_when_critic_down(monkeypatch, series):
    def fake_llm(messages, tier, json_mode, max_tokens, caller):
        if caller == "anthology_story":
            return json.dumps({"story": "история", **BUBLIK["case"]}, ensure_ascii=False)
        if caller == "anthology_critic":
            raise RuntimeError("quota")
        return _screenplay(BUBLIK["lines"])

    monkeypatch.setattr(aa, "_llm", fake_llm)
    ep = aa.write_from_pitch(series, PITCH)
    assert ep["critic"]["score"] is None and len(ep["lines"]) == len(BUBLIK["lines"])


def test_api_pitches_vote_and_jobs(tmp_path):
    out = _api_check("""
        api.DB_PATH = %r
        api.JOBS_DIR = __import__("pathlib").Path(%r)
        from agents import anthology_agent as aa
        ids = aa.save_pitches(aa.load_series("kabinet7"), [%r], "b", api.DB_PATH)
        out["vote"] = client.post(f"/api/pitches/{ids[0]}", json={"vote": 1}).status_code
        out["bad_vote"] = client.post(f"/api/pitches/{ids[0]}", json={"vote": 5}).status_code
        out["list"] = [p["vote"] for p in client.get("/api/pitches?series=kabinet7").get_json()]
        with patch.object(api.subprocess, "Popen") as popen, patch.object(api.threading, "Thread"):
            popen.return_value.pid = 1
            client.post("/api/jobs", json={"type": "pitches", "series": "kabinet7", "n": 12})
            out["pitches_cmd"] = popen.call_args.args[0][4:]
            client.post("/api/jobs", json={"type": "lab", "series": "kabinet7", "pitch_ids": [ids[0], "x"]})
            out["lab_cmd"] = popen.call_args.args[0][4:]
    """ % (str(tmp_path / "p.db"), str(tmp_path / "jobs"), PITCH), tmp_path)
    assert out["vote"] == 200 and out["bad_vote"] == 400 and out["list"] == [1]
    assert out["pitches_cmd"] == ["--series", "kabinet7", "--pitches", "--n", "12"]
    assert out["lab_cmd"][-2:] == ["--from-pitches", "1"] and "--same" in out["lab_cmd"]


def test_critic_gets_audience_reveals_and_drop_line(monkeypatch, series):
    seen = {}

    def fake_llm(messages, tier, json_mode, max_tokens, caller):
        seen.update(tier=tier, prompt=messages[-1]["content"])
        return json.dumps({"checks": {"reveals": False, "turn": True}, "watch": 6, "drop_line": 5,
                           "fixes": ["реплика 5: объяснение вместо открытия"]}, ensure_ascii=False)

    monkeypatch.setattr(aa, "_llm", fake_llm)
    review = aa.critique_episode({"lines": BUBLIK["lines"]}, {**series, "audience": "взрослые 30+."})
    assert seen["tier"] == "critic" and "Зритель: взрослые 30+." in seen["prompt"]
    assert "- reveals:" in seen["prompt"]
    assert review == {"score": 6, "failed": ["reveals"], "fixes": ["реплика 5: объяснение вместо открытия"],
                      "drop_line": 5, "script": None}
    monkeypatch.setattr(aa, "_llm", lambda *a, **k: '{"watch": 7, "drop_line": 99}')
    assert aa.critique_episode({"lines": BUBLIK["lines"]})["drop_line"] is None   # номер вне сценария


def test_story_reveals_reach_beats_prompt(monkeypatch, series):
    prompts = {}

    def fake_llm(messages, tier, json_mode, max_tokens, caller):
        prompts.setdefault(caller, messages[-1]["content"])
        if caller == "anthology_story":
            return json.dumps({"story": "история", "reveals": ["носит пустой кофр", "скрипка продана"],
                               **BUBLIK["case"]}, ensure_ascii=False)
        if caller == "anthology_critic":
            return json.dumps({"checks": {k: True for k in aa._CRITIC_CHECKS}, "watch": 9, "fixes": []})
        return _screenplay(BUBLIK["lines"])

    monkeypatch.setattr(aa, "_llm", fake_llm)
    ep = aa.write_from_pitch(series, PITCH)
    assert "1. носит пустой кофр\n2. скрипка продана" in prompts["anthology_episode"]
    assert "ЗРИТЕЛЬ: " in prompts["anthology_episode"] and ep["reveals"] == ["носит пустой кофр", "скрипка продана"]


def test_critic_tier_starts_with_kie_critics(monkeypatch):
    from utils import llm_client
    monkeypatch.setenv("SERIES_CRITIC_MODEL", "codex:gpt-x")
    monkeypatch.delenv("SERIES_CRITIC_FALLBACK", raising=False)
    labels = [label for _gate, label, _fn in llm_client._chain("critic", False)]
    assert labels[:2] == ["kie:codex:gpt-x", "kie:claude:claude-fable-5"]
    assert not any(l.startswith("kie:gemini:gemini-4-argon") for l in labels)   # сценарист себя не судит

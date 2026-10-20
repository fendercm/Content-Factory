"""Тесты сериального пайплайна без сети: разбор сценария, раскадровка, таймлайн, озвучка, KIE."""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from agents import anthology_agent as aa

SERIES = aa.load_series("kabinet7")

SCREENPLAY = """НАЗВАНИЕ: Отчёт на лежаке

СЦЕНАРИЙ:
АЛИНА: Утюг выключила. Окна закрыла. А отчёт — нет.
АЛИНА: Прилетела в понедельник. Хотя билет был на четверг.
ВЕРА: Загорели?
АЛИНА: Не успела.
ВЕРА: Сами билет поменяли?
АЛИНА: Доплатила одиннадцать тысяч. Лежать у моря и считать — глупо же.
ВЕРА: Что вы считали?
АЛИНА: Чат проверяла. Раз сорок в день. Муж сказал: ты не со мной, ты с телефоном.
ВЕРА: Вы четвёртый раз закрываете молнию. Сумка пустая.
РАССКАЗЧИК: Вечером под дверью седьмого кабинета лежала записка.

КАДРЫ:
1. [ОЖИВИТЬ] Крупно: руки тянут молнию сумки.
2. Кабинет №7, вечерний свет.
3. [ОЖИВИТЬ] Пальцы защёлкивают колпачок.
4. Крупно лицо Веры.
5. [ОЖИВИТЬ] Флешбэк: лежак у моря.
6. Алина замирает.
7. Общий план: Алина уходит.
8. [ОЖИВИТЬ] Вера поднимает листок.
"""


def test_parse_screenplay_roles_and_shots():
    ep = aa.parse_screenplay(SCREENPLAY, "АЛИНА")
    assert ep["title"] == "Отчёт на лежаке"
    assert [l["role"] for l in ep["lines"]][:3] == ["АЛИНА", "АЛИНА", "ВЕРА"]
    assert ep["lines"][-1]["role"] == "РАССКАЗЧИК"
    assert len(ep["shots"]) == 8
    assert sum(s["animate"] for s in ep["shots"]) == 4


def test_validate_episode_flags_short_script():
    ep = aa.parse_screenplay(SCREENPLAY, "АЛИНА")
    problems = aa.validate_episode(ep, SERIES)
    assert any("длина" in p for p in problems)  # в тестовом сценарии ~70 слов


def test_validate_flags_monologue_and_missing_name():
    lines = [{"role": "МАРИНА", "text": "Я купила тринадцатый курс, лишь бы не называть цену."}] * 3 + \
            [{"role": "ВЕРА", "text": "Сколько стоит ваш час?"}] * 9
    ep = {"lines": lines, "case": {"client_name": "Марина"}}
    problems = " | ".join(aa.validate_episode(ep, SERIES))
    assert "подряд" in problems and "имя клиента" in problems
    ep["lines"] = [{"role": "МАРИНА" if i % 2 == 0 else "ВЕРА",
                    "text": "Марина, сколько стоит ваш час?" if i == 1 else "реплика"} for i in range(12)]
    problems = " | ".join(aa.validate_episode(ep, SERIES))
    assert "подряд" not in problems and "имя клиента" not in problems


def _plan_input():
    roles = ["АЛИНА", "ВЕРА", "АЛИНА", "ВЕРА", "АЛИНА", "ВЕРА", "АЛИНА", "ВЕРА", "АЛИНА", "РАССКАЗЧИК"]
    lines = [{"role": r, "text": f"реплика {i}"} for i, r in enumerate(roles)]
    episode = {"lines": lines, "client_role": "АЛИНА",
               "case": {"age": 31, "gender": "female", "occupation": "бухгалтер"}}
    data = {"client_visual_en": "Alina", "turn_line": 6, "detail_line": 3, "story_line": None,
            "shots": {slot: {"prompt_en": slot, "motion_en": "m"} for slot, _ in aa.SLOTS}}
    return episode, data


def test_build_plan_puts_speaker_on_screen():
    episode, data = _plan_input()
    plan = aa.build_plan(data, episode, key=2, simple=2)
    slots = [a["slot"] for a in plan["assignment"]]
    assert slots[0] == "client_open" and slots[1] == "wide" and slots[3] == "detail"
    assert slots[5] == "vera_ask"            # вопрос перед поворотом
    assert slots[6] == slots[8] == "client_turn"
    assert slots[9] == "story"               # рассказчик
    for line, slot in zip(episode["lines"], slots):
        if slot.startswith("vera_"):
            assert line["role"] == "ВЕРА"
        if slot.startswith("client_"):
            assert line["role"] == "АЛИНА"
    # кадры — только используемые слоты, анимация — по одному клипу на анимированный слот
    assert {s["slot"] for s in plan["shots"]} == set(slots)
    animated = {s["slot"]: s["priority"] for s in plan["shots"] if s["animate"]}
    assert animated == {"client_turn": "key", "client_open": "key", "detail": "simple", "vera_ask": "simple"}
    clips = [a for a in plan["assignment"] if a["clip"]]
    assert len({a["slot"] for a in clips}) == len(clips)
    assert plan["assignment"][5]["clip"] and plan["assignment"][6]["clip"]
    assert all(plan["shots"][a["shot"]]["slot"] == a["slot"] for a in plan["assignment"])


def test_build_plan_rejects_incomplete_storyboard():
    episode, data = _plan_input()
    data["shots"] = {"wide": {"prompt_en": "x"}}
    with pytest.raises(ValueError):
        aa.build_plan(data, episode, key=2, simple=2)


def test_build_timeline_cuts_on_speaker_and_merges():
    from agents.series_assembly import build_timeline
    lines = [{"start": t, "end": t + 1.5} for t in (0, 2, 2.4, 6, 9, 13)]
    assignment = [{"shot": s, "clip": c} for s, c in
                  [(0, True), (1, False), (2, False), (2, True), (1, False), (3, False)]]
    tl = build_timeline(assignment, lines, speech_end=15.0, tail=0.8)
    assert [seg["index"] for seg in tl] == [0, 2, 1, 3]   # 0.4-секундный кадр 1 ушёл в предыдущий
    assert tl[0]["start"] == 0.0 and tl[1]["clip"]         # клип со склеенной реплики сохранился
    assert all(seg["duration"] >= 1.0 for seg in tl)
    assert sum(seg["duration"] for seg in tl) == pytest.approx(15.8)


def test_dialogue_ass_speaker_labels(tmp_path):
    from agents.subtitle_agent import write_dialogue_ass
    lines = [{"role": "ВЕРА", "text": "Марина, что вы проверяете?", "start": 0.0, "end": 2.0},
             {"role": "МАРИНА", "text": "Кольцо.", "start": 2.3, "end": 3.0},
             {"role": "ВЕРА", "text": "Четвёртый раз.", "start": 3.3, "end": 4.5},
             {"role": "РАССКАЗЧИК", "text": "Вечером под дверью лежала записка.", "start": 5.0, "end": 7.0}]
    path = tmp_path / "s.ass"
    write_dialogue_ass(lines, str(path), {"ВЕРА": "&H00FFFFFF&", "МАРИНА": "&H0000E5FF&"},
                       labels={"ВЕРА": "ВЕРА", "МАРИНА": "МАРИНА"},
                       intro_labels={"ВЕРА": "Вера Соколова · психолог", "МАРИНА": "Марина, 38 · коуч"})
    speaker = [l for l in path.read_text(encoding="utf-8").splitlines() if l.startswith("Dialogue: 2,")]
    assert len(speaker) == 3  # у рассказчика подписи нет
    assert speaker[0].endswith("ВЕРА СОКОЛОВА · ПСИХОЛОГ") and speaker[1].endswith("МАРИНА, 38 · КОУЧ")
    assert speaker[2].endswith("}ВЕРА")


def test_dialogue_chunks_split_time_proportionally():
    from agents.subtitle_agent import _dialogue_chunks
    chunks = _dialogue_chunks("Утюг выключила. Окна закрыла. А отчёт — нет.", 1.0, 3.0, 14)
    assert chunks[0]["start"] == 1.0 and chunks[-1]["end"] == pytest.approx(3.0)
    assert all(len(c["text"].split()) <= 3 for c in chunks)


def test_kie_check_raises_and_marks_dead_on_402():
    from utils import kie_client, providers
    providers._dead.clear()
    with pytest.raises(kie_client.KieError):
        kie_client._check({"code": 402, "msg": "Credits insufficient"}, "createTask")
    assert providers.dead_reason("kie_series") and providers.dead_reason("kie")
    providers._dead.clear()
    with pytest.raises(kie_client.KieError):
        kie_client._check({"type": "error", "error": {"message": "Internal error"}}, "chat")


def test_budget_allow_enables_only_series_kie(monkeypatch):
    from utils import providers
    monkeypatch.setenv("BUDGET_MODE", "free")
    monkeypatch.delenv("BUDGET_ALLOW", raising=False)
    assert providers.is_enabled("kie_series") is False
    monkeypatch.setenv("BUDGET_ALLOW", "kie_series")
    assert providers.is_enabled("kie_series") is True
    # старые платные пути через KIE (Kling в visual_agent, ElevenLabs, Suno) остаются выключены
    assert providers.is_enabled("kie") is False
    assert providers.is_enabled("elevenlabs") is False


def _episode():
    ep = aa.parse_screenplay(SCREENPLAY, "АЛИНА")
    ep.update({"case": {"gender": "female"}, "client_role": "АЛИНА"})
    return ep


def test_dialogue_voice_timings_and_pauses(tmp_path, monkeypatch):
    from agents import dialogue_voice as dv
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_TTS", "on")
    tone = lambda text, voice: np.ones(int(0.5 * dv.SAMPLE_RATE), dtype=np.float32) * 0.5  # noqa: E731
    with patch.object(dv, "_gemini_pcm", side_effect=tone):
        res = dv.synthesize(SERIES, _episode(), tmp_path)
    lines = res["lines"]
    assert res["engine"] == "gemini" and len(lines) == 10
    assert lines[1]["start"] - lines[0]["end"] == pytest.approx(dv.PAUSE_AFTER_COLD_OPEN, abs=0.01)
    assert lines[2]["start"] - lines[1]["end"] == pytest.approx(dv.PAUSE_SPEAKER_CHANGE, abs=0.01)
    assert (tmp_path / "dialogue.wav").exists()


def test_dialogue_voice_switches_whole_episode_to_edge(tmp_path, monkeypatch):
    from agents import dialogue_voice as dv
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    calls = {"n": 0}

    def flaky(text, voice):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("429 PerDay")
        return np.ones(1200, dtype=np.float32) * 0.3

    edge = MagicMock(return_value=np.ones(1200, dtype=np.float32) * 0.3)
    with patch.object(dv, "_gemini_pcm", side_effect=flaky), patch.object(dv, "_edge_pcm", edge):
        res = dv.synthesize(SERIES, _episode(), tmp_path)
    assert res["engine"] == "edge"
    assert edge.call_count == 10  # переозвучен весь эпизод, а не только упавшая реплика


def test_premium_tier_falls_back_from_kie(monkeypatch):
    from utils import llm_client, providers
    providers._dead.clear()
    monkeypatch.setenv("BUDGET_ALLOW", "kie_series")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    ok = {"candidates": [{"content": {"parts": [{"text": "резерв"}]}}]}
    resp = MagicMock(status_code=200); resp.json.return_value = ok
    with patch("utils.kie_client.chat", side_effect=RuntimeError("524")) as kie_chat, \
         patch.object(llm_client._requests, "post", return_value=resp), \
         patch.object(llm_client, "_track"):
        out = llm_client.chat([{"role": "user", "content": "x"}], tier="premium")
    assert kie_chat.call_count == 2  # Argon, затем Astra
    assert out.provider == "gemini" and out.choices[0].message.content == "резерв"

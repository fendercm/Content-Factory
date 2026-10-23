"""Тесты сериального пайплайна без сети: разбор сценария, раскадровка, таймлайн, озвучка, KIE."""

from itertools import accumulate
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
    # оживляются только кадры без говорящих лиц: первые key — Kling, следующие — Wan
    assert animated == {"detail": "key", "wide": "key", "story": "simple"}
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
        res = dv.synthesize({**SERIES, "tts_mode": "lines", "voice_tempo": 1.0}, _episode(), tmp_path)
    lines = res["lines"]
    assert res["engine"] == "gemini" and len(lines) == 10
    assert lines[1]["start"] - lines[0]["end"] == pytest.approx(dv.PAUSE_AFTER_COLD_OPEN, abs=0.01)
    assert lines[2]["start"] - lines[1]["end"] == pytest.approx(dv.PAUSE_SPEAKER_CHANGE, abs=0.01)
    assert (tmp_path / "dialogue.wav").exists()


def _tone(seconds):
    return np.ones(int(seconds * 24000), dtype=np.float32) * 0.5


def test_dialogue_mode_one_request_plus_narrator(tmp_path, monkeypatch):
    from agents import dialogue_voice as dv
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    episode = _episode()                       # 9 реплик Алины и Веры + рассказчик последним
    spoken = [l for l in episode["lines"] if l["role"] != "РАССКАЗЧИК"]
    # Реплики по 1 с подряд, между первой и второй — 0.2 с тишины
    bounds = [(0.0, 1.0)] + [(1.2 + i * 1.1, 2.2 + i * 1.1) for i in range(len(spoken) - 1)]
    words = [[{"word": "слово", "start": s, "end": e}] for s, e in bounds]
    tts = MagicMock(side_effect=[_tone(12.0), _tone(2.0)])
    with patch.object(dv, "_gemini_tts", tts), patch.object(dv, "_line_words", return_value=words):
        res = dv.synthesize({**SERIES, "voice_tempo": 1.0}, episode, tmp_path)  # паузы — без ускорения
    assert res["engine"] == "gemini-dialogue" and tts.call_count == 2   # диалог + рассказчик
    text, config = tts.call_args_list[0].args
    speakers = config["multiSpeakerVoiceConfig"]["speakerVoiceConfigs"]
    assert {s["speaker"] for s in speakers} == {"Алина", "Вера"}
    assert text.startswith(SERIES["tts_direction"])                       # режиссёрское вступление
    assert text.split("\n\n", 1)[1].startswith("Алина: ") and "РАССКАЗЧИК" not in text
    lines = res["lines"]
    assert lines[1]["start"] - lines[0]["end"] == pytest.approx(dv.PAUSE_AFTER_COLD_OPEN, abs=0.01)
    assert lines[-1]["role"] == "РАССКАЗЧИК" and lines[-1]["end"] == pytest.approx(res["duration"], abs=0.01)
    # слова для субтитров сдвинуты вместе с репликой на паузу после cold open
    assert lines[1]["words"][0]["start"] == pytest.approx(lines[1]["start"], abs=0.01)
    assert "words" not in lines[-1]


def test_dialogue_mode_falls_back_to_lines_when_alignment_fails(tmp_path, monkeypatch):
    from agents import dialogue_voice as dv
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    with patch.object(dv, "_gemini_tts", return_value=_tone(5.0)), \
         patch.object(dv, "_line_words", side_effect=dv._AlignError("не совпало")), \
         patch.object(dv, "_gemini_pcm", return_value=_tone(0.5)) as per_line:
        res = dv.synthesize(SERIES, _episode(), tmp_path)
    assert res["engine"] == "gemini" and per_line.call_count == 10


def test_series_without_fallback_stops_on_gemini_failure(tmp_path, monkeypatch):
    from agents import dialogue_voice as dv
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    with patch.object(dv, "_gemini_tts", side_effect=RuntimeError("429 PerDay")), \
         patch.object(dv, "_edge_pcm") as edge:
        with pytest.raises(RuntimeError, match="voice_fallback=none"):
            dv.synthesize(SERIES, _episode(), tmp_path)
    assert not edge.called


def test_line_bounds_maps_whisper_words_to_lines(tmp_path):
    from agents import dialogue_voice as dv
    lines = [{"role": "А", "text": "Добрый день, Вера."}, {"role": "Б", "text": "Садитесь."}]
    words = [{"word": " Добрый", "start": 0.1, "end": 0.4}, {"word": " день,", "start": 0.4, "end": 0.7},
             {"word": " Вера.", "start": 0.7, "end": 1.1}, {"word": " Садитесь.", "start": 1.6, "end": 2.2}]
    model = MagicMock()
    model.transcribe.return_value = {"segments": [{"words": words}]}
    with patch("agents.subtitle_agent._get_whisper_model", return_value=model):
        bounds = dv._line_bounds(_tone(2.5), lines, tmp_path)
    assert bounds == [(0.1, 1.1), (1.6, 2.2)]


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
        res = dv.synthesize({**SERIES, "tts_mode": "lines", "voice_fallback": "edge"}, _episode(), tmp_path)
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


def test_build_plan_animates_only_faceless_shots():
    episode, data = _plan_input()
    plan = aa.build_plan(data, episode, key=0, simple=4)
    animated = {s["slot"]: s["priority"] for s in plan["shots"] if s["animate"]}
    assert animated == {"detail": "simple", "wide": "simple", "story": "simple"}
    assert not any(s["animate"] for s in plan["shots"] if s["slot"].startswith(("client_", "vera_")))


def test_kie_download_retries_network_errors(tmp_path):
    import requests
    from utils import kie_client
    calls = {"n": 0}

    def flaky(url, dest, timeout, min_bytes):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.ConnectionError("reset")
        open(dest, "wb").write(b"x" * 2000)
        return dest

    with patch("utils.download.download_to", side_effect=flaky), patch.object(kie_client.time, "sleep"):
        out = kie_client.download("https://cdn/x.jpg", tmp_path / "x.jpg")
    assert out.exists() and calls["n"] == 3


def test_copied_from_brief_catches_phrases_from_instructions():
    ep = {"lines": [{"role": "ВЕРА", "text": "Марина, с чем вы пришли?"},
                    {"role": "МАРИНА", "text": "Мне нужен был простой человеческий вопрос и всё."}]}
    assert aa.copied_from_brief(ep) == "Мне нужен был простой человеческий вопрос и всё."
    ep["lines"].pop()
    assert aa.copied_from_brief(ep) is None


def test_dialogue_chunks_follow_spoken_words():
    from agents.subtitle_agent import _dialogue_chunks
    words = [{"word": w, "start": s, "end": s + 0.3} for w, s in
             [("Игорь,", 1.0), ("с", 1.4), ("чем", 1.6), ("вы", 1.9), ("пришли?", 2.2)]]
    chunks = _dialogue_chunks("Игорь, с чем вы пришли?", 1.0, 2.6, 18, words=words)
    assert [c["text"] for c in chunks] == ["Игорь,", "с чем вы", "пришли?"]      # разрыв после запятой
    assert [c["start"] for c in chunks] == [1.0, 1.4, 2.2]                       # по реальным словам
    assert chunks[-1]["end"] == 2.6


def test_repeated_phrase_blocks_lines_that_say_the_same():
    lines = [{"role": "ИГОРЬ", "text": "Сотрудники уже всё без меня могут."},
             {"role": "ВЕРА", "text": "Игорь, кто встаёт, когда вы входите?"},
             {"role": "ИГОРЬ", "text": "Тогда зачем... Сотрудники уже всё без меня могут."}]
    assert aa.repeated_phrase(lines) == "сотрудники уже все без"
    assert aa.repeated_phrase(lines[:2]) is None
    ep = {"lines": lines * 4, "case": {"client_name": "Игорь"}}
    assert any("повторяется" in p for p in aa.validate_episode(ep, SERIES))


def _confession():
    roles = ["ДЕНИС", "ВЕРА", "ДЕНИС", "ДЕНИС", "ВЕРА", "ДЕНИС", "ДЕНИС", "ВЕРА", "ДЕНИС", "ВЕРА"]
    lines = [{"role": r, "text": " ".join(f"слово{i}_{k}" for k in range(11))} for i, r in enumerate(roles)]
    lines[1]["text"] = "Денис, " + lines[1]["text"]
    return {"lines": lines, "case": {"client_name": "Денис", "occupation": "столяр"}, "client_role": "ДЕНИС"}


def test_polish_keeps_original_when_editor_breaks_roles():
    ep = _confession()
    swapped = "\n".join(f"{'ВЕРА' if l['role'] == 'ДЕНИС' else 'ДЕНИС'}: {l['text']}" for l in ep["lines"])
    with patch.object(aa, "_llm", return_value=swapped):
        assert aa.polish_episode(SERIES, ep) is ep


def test_polish_accepts_valid_rewrite():
    ep = _confession()
    rewritten = "\n".join(f"{l['role']}: {l['text'].replace('слово', 'речь')}" for l in ep["lines"])
    with patch.object(aa, "_llm", return_value=rewritten):
        out = aa.polish_episode(SERIES, ep)
    assert out is not ep and out["lines"][0]["text"].startswith("речь0_0")
    assert [l["role"] for l in out["lines"]] == [l["role"] for l in ep["lines"]]


def test_roles_mixed_reports_lines_and_fails_open():
    ep = _confession()
    with patch.object(aa, "_llm", return_value='{"ok": false, "lines": [4, 7]}'):
        assert aa.roles_mixed(SERIES, ep) == [4, 7]
    with patch.object(aa, "_llm", side_effect=RuntimeError("503")):
        assert aa.roles_mixed(SERIES, ep) == []          # проверка недоступна — эпизод не блокируем


def test_screenplay_emotion_tags_go_to_voice_not_subtitles():
    ep = aa.parse_screenplay("НАЗВАНИЕ: Т\nСЦЕНАРИЙ:\nИГОРЬ: [устало, тихо] Я дома.\nВЕРА: Игорь?", "ИГОРЬ")
    assert ep["lines"][0] == {"role": "ИГОРЬ", "text": "Я дома.", "emotion": "устало, тихо"}
    assert "emotion" not in ep["lines"][1]
    from agents import dialogue_voice as dv
    with patch.object(dv, "_gemini_tts", return_value=np.zeros(10, dtype=np.float32)) as tts:
        dv._gemini_dialogue_pcm(ep["lines"], {"ИГОРЬ": {"gemini": "Orus"}, "ВЕРА": {"gemini": "Gacrux"}}, "Сыграй.")
    assert tts.call_args.args[0] == "Сыграй.\n\nИгорь: [устало, тихо] Я дома.\nВера: Игорь?"


def test_lipsync_key_lines_and_apply():
    from agents import lipsync_agent as ls
    lines = [{"role": r, "text": "x"} for r in ["АРТЁМ", "ВЕРА", "АРТЁМ", "ВЕРА", "АРТЁМ", "РАССКАЗЧИК"]]
    plan = {"turn_line": 2, "assignment": [{"slot": s, "shot": i} for i, s in
            enumerate(["client_open", "wide", "client_turn", "vera_ask", "client_talk", "story"])]}
    assert ls.key_lines(lines, plan) == [0, 2, 4]          # первая, поворот, последняя реплика героев
    plan["assignment"][0]["slot"] = "detail"               # без лица — не оживляем
    assert ls.key_lines(lines, plan) == [2, 4]
    shots = [{"slot": a["slot"], "image_path": f"{i}.jpg"} for i, a in enumerate(plan["assignment"])]
    out = ls.apply(plan["assignment"], shots, {2: "lip_02.mp4"})
    assert out[2]["slot"] == "lipsync_2" and out[2]["clip"] is True
    assert shots[out[2]["shot"]] == {"slot": "lipsync_2", "image_path": "2.jpg", "video_path": "lip_02.mp4",
                                     "engine": "lipsync", "lipsync": True}
    assert plan["assignment"][2]["slot"] == "client_turn"  # исходное назначение не тронуто


def test_dialogue_turns_merge_same_speaker_and_extra_speech_detected():
    from agents import dialogue_voice as dv

    lines = [{"role": "АРТЁМ", "text": "Сегодня два года."},
             {"role": "АРТЁМ", "text": "Утром пришло фото.", "emotion": "шёпотом"},
             {"role": "ВЕРА", "text": "Она знает."}]
    assert [len(t) for t in dv._turns(lines)] == [2, 1]
    words = lambda s: [{"word": w} for w in s.split()]
    assert dv._extra_speech(words("Сегодня два года шёпотом со слезами утром пришло фото она знает"), lines) \
        == "шепотом со слезами"
    assert dv._extra_speech(words("Сегодня два года утром пришла фото она знает"), lines) == ""


def test_voice_cue_keeps_voice_drops_actions():
    from agents import dialogue_voice as dv

    assert dv._voice_cue("глухо, глядя в пол") == "глухо"
    assert dv._voice_cue("теребит ручку") == ""
    assert dv._voice_cue("голос срывается, почти шёпотом") == "голос срывается, почти шёпотом"
    assert dv._voice_cue("тепло, чуть улыбнувшись") == "тепло"
    assert dv._speaker_name("ВИКТОР ПАВЛОВИЧ") == "Виктор"


def test_kie_run_task_retries_only_transient(monkeypatch):
    from utils import kie_client as kc

    calls = []

    def fake_wait(task_id, timeout_s, model=""):
        calls.append(task_id)
        if len(calls) < 3:
            raise kc.KieError("задача kling/ai-avatar-standard: 500 internal error, please try again later.")
        return {"urls": ["u"], "credits": 8}

    monkeypatch.setattr(kc, "create_task", lambda model, input_: f"t{len(calls)}")
    monkeypatch.setattr(kc, "wait_task", fake_wait)
    monkeypatch.setattr(kc.time, "sleep", lambda s: None)
    assert kc.run_task("m", {}, retries=2)["urls"] == ["u"] and len(calls) == 3

    calls.clear()
    monkeypatch.setattr(kc, "wait_task", lambda *a, **k: (_ for _ in ()).throw(
        kc.KieError("createTask m: 402 Credits insufficient")))
    try:
        kc.run_task("m", {}, retries=2)
        raise AssertionError("402 не повторяется")
    except kc.KieError:
        pass
    assert kc._transient(kc.KieError("задача m: 500 Internal Error")) and \
        not kc._transient(kc.KieError("задача m не завершилась за 1500 с"))


def test_scale_timing_speeds_lines_and_words():
    from agents.dialogue_voice import scale_timing
    lines = [{"role": "ВЕРА", "text": "Да.", "start": 1.15, "end": 2.3,
              "words": [{"word": "Да.", "start": 1.15, "end": 2.3}]},
             {"role": "ЛЕВ", "text": "Нет.", "start": 2.3, "end": 3.45}]
    out = scale_timing(lines, 1.15)
    assert (out[0]["start"], out[0]["end"]) == (1.0, 2.0) and out[0]["words"][0]["end"] == 2.0
    assert out[1]["end"] == 3.0 and "words" not in out[1] and lines[0]["start"] == 1.15  # исходник не тронут


def test_apply_tempo_shortens_track(tmp_path):
    import wave
    from agents import dialogue_voice as dv
    path = tmp_path / "d.wav"
    dv._write_wav(np.sin(np.linspace(0, 2000, dv.SAMPLE_RATE * 2)).astype(np.float32), path)
    dialogue = {"audio_path": str(path), "duration": 2.0, "engine": "edge",
                "lines": [{"role": "ВЕРА", "text": "Да.", "start": 0.0, "end": 2.0}]}
    assert dv.apply_tempo(dialogue, 1.0) is dialogue
    fast = dv.apply_tempo(dialogue, 1.25)
    with wave.open(fast["audio_path"]) as w:
        assert w.getnframes() / w.getframerate() == pytest.approx(1.6, abs=0.05)
    assert fast["duration"] == pytest.approx(1.6, abs=0.05) and fast["lines"][0]["end"] == 1.6


def test_plan_pieces_splits_long_stills_and_alternates_framing():
    from agents.series_assembly import plan_pieces
    shots = [{"image_path": "a.png"}, {"image_path": "b.png", "video_path": "b.mp4"}, {"image_path": "c.png"}]
    timeline = [{"index": 0, "duration": 7.0, "clip": False}, {"index": 1, "duration": 5.0, "clip": True},
                {"index": 2, "duration": 2.5, "clip": False}, {"index": 0, "duration": 2.0, "clip": False}]
    pieces = plan_pieces(timeline, shots, max_still=3.0)
    assert [(p["index"], p["punch"]) for p in pieces] == [
        (0, False), (0, True), (0, False),  # 7 с → три куска, крупность чередуется
        (1, False),                          # клип не режется
        (2, False),
        (0, True)]                           # возврат к кадру — продолжение чередования
    assert pieces[0]["duration"] == pytest.approx(7 / 3) and pieces[3]["clip"]
    assert sum(p["duration"] for p in pieces) == pytest.approx(16.5)
    # без max_still — как раньше: кусок на отрезок, без наезда
    assert [p["punch"] for p in plan_pieces(timeline, shots)] == [False] * 4


def test_dialogue_ass_question_until_turn(tmp_path):
    from agents.subtitle_agent import write_dialogue_ass
    lines = [{"role": "ЛЕВ", "text": "Скрипку я продал.", "start": 0.0, "end": 2.0},
             {"role": "ВЕРА", "text": "Зачем?", "start": 2.3, "end": 3.0},
             {"role": "ЛЕВ", "text": "Я не слышу левым ухом.", "start": 3.3, "end": 5.0}]
    path = tmp_path / "s.ass"
    write_dialogue_ass(lines, str(path), {}, question="Зачем он носит гантели в кофре?", question_end=3.3)
    events = [l for l in path.read_text(encoding="utf-8").splitlines() if ",Question," in l]
    assert len(events) == 1 and events[0].startswith("Dialogue: 3,0:00:00.00,0:00:03.30,Question,")
    assert events[0].endswith("Зачем он носит гантели в кофре?")
    write_dialogue_ass(lines, str(path), {})
    assert ",Question," not in path.read_text(encoding="utf-8").split("[Events]")[1]


def test_hook_question_rejects_spoiler_and_bad_format():
    episode = {"lines": [{"role": "ЛЕВ", "text": "Скрипку я продал."},
                         {"role": "ВЕРА", "text": "Зачем?"},
                         {"role": "ЛЕВ", "text": "Я не слышу левым ухом."}]}
    answers = iter(['{"question": "«Зачем он носит гантели в кофре?»"}',
                    '{"question": "Почему он не слышит левым ухом?"}',
                    '{"question": "Что он скрывает"}'])
    with patch.object(aa, "_llm", side_effect=lambda *a, **k: next(answers)):
        assert aa.hook_question(SERIES, episode, turn_line=2) == "Зачем он носит гантели в кофре?"
        assert aa.hook_question(SERIES, episode, turn_line=2) is None   # пересказывает поворот
        assert aa.hook_question(SERIES, episode, turn_line=2) is None   # не вопрос
    with patch.object(aa, "_llm", side_effect=RuntimeError("нет сети")):
        assert aa.hook_question(SERIES, episode) is None


def test_build_plan_memory_scenes_on_client_story_lines():
    episode, data = _plan_input()
    data["shots"]["memory_1"].update(setting_en="a restaurant cloakroom", with_client=True)
    data["shots"]["memory_3"] = {}                       # без описания — сцены нет
    # 0 — первая реплика, 6 — поворот, 1 — реплика Веры, 2 — повтор: всё это не сцены
    data["memory_lines"] = [2, 4, 0]
    plan = aa.build_plan(data, episode, key=0, simple=3)
    slots = [a["slot"] for a in plan["assignment"]]
    assert slots[2] == "memory_1" and slots[4] == "memory_2" and "memory_3" not in slots
    assert slots[0] == "client_open" and slots[6] == "client_turn"
    assert plan["memory_lines"] == [2, 4]
    shots = {s["slot"]: s for s in plan["shots"]}
    assert shots["memory_1"]["characters"] == ["CLIENT"] and shots["memory_1"]["setting_en"] == "a restaurant cloakroom"
    assert shots["memory_2"]["characters"] == []          # клиента в кадре нет — без его референса
    # оживление — сначала сцены из жизни, потом деталь
    assert {s for s, sh in shots.items() if sh["animate"]} == {"memory_1", "memory_2", "detail"}
    data["memory_lines"] = [2, 8, 3]                      # 8 — после поворота, 3 — деталь
    slots = [a["slot"] for a in aa.build_plan(data, episode, key=0, simple=3)["assignment"]]
    assert slots.count("memory_1") == 1 and "memory_2" not in slots and slots[3] == "detail"


def test_scene_prompt_uses_memory_setting():
    from agents.scene_agent import _shot_prompt
    plan = {"client_visual_en": "Grigory"}
    office = _shot_prompt(SERIES, plan, {"characters": [], "prompt_en": "wide"})
    memory = _shot_prompt(SERIES, plan, {"characters": ["CLIENT"], "prompt_en": "coats", "setting_en": "cloakroom"})
    assert SERIES["location_en"] in office and "Setting: cloakroom." in memory and SERIES["location_en"] not in memory
    assert "CLIENT (reference image 1): Grigory" in memory


def test_detail_moves_off_first_turn_and_last_line():
    """detail_line=0 (или null, или поворот) — кадр-деталь не пропадает, а встаёт на следующую реплику
    клиента: не первую, не поворот, не последнюю и, если можно, не занятую сценой из жизни."""
    episode, data = _plan_input()   # клиент: 0, 2, 4, 6 (поворот), 8 (последняя)
    for detail, memory, expected in [(0, [], 2), (None, [], 2), (0, [2], 4), (6, [], 2), (8, [], 2), (2, [], 2)]:
        plan = aa.build_plan({**data, "detail_line": detail, "memory_lines": memory}, episode, key=0, simple=3)
        slots = [a["slot"] for a in plan["assignment"]]
        assert slots.count("detail") == 1 and slots[expected] == "detail", (detail, memory, slots)
        assert slots[0] == "client_open" and slots[6] == "client_turn" and slots[8] == "client_turn"
        assert plan["detail_line"] == expected
    # деталь на реплике Веры — как поставил раскадровщик
    slots = [a["slot"] for a in aa.build_plan({**data, "detail_line": 3}, episode, key=0, simple=3)["assignment"]]
    assert slots[3] == "detail"


def test_memory_scene_prompt_drops_office_light_keeps_series_look():
    from agents.scene_agent import SCENE_LIGHT, _shot_prompt, scene_style
    plan = {"client_visual_en": "Grigory"}
    office = _shot_prompt(SERIES, plan, {"characters": [], "prompt_en": "wide"})
    memory = _shot_prompt(SERIES, plan, {"characters": ["CLIENT"], "prompt_en": "coats", "setting_en": "cloakroom"})
    assert "desk-lamp" in office and SCENE_LIGHT not in office          # кабинет — как раньше
    assert "desk-lamp" not in memory and "window" not in memory and SCENE_LIGHT in memory
    for keep in ("Photorealistic cinematic still", "35mm lens", "teal and amber", "consistent character appearance",
                 "No text, no captions, no watermark.", "keep each face"):
        assert keep in memory
    assert scene_style("Cinematic, warm lamp light. No text.") == "Cinematic. No text."


def test_storyboard_dark_light_is_brightened():
    """Раскадровщик пишет «Dimly lit», хотя просим светло: тёмный свет заменяется, цвет волос и одежды — нет."""
    assert aa.brighten("Dimly lit kitchen at night, moody shadows.") == "brightly lit kitchen in the early evening, soft shadows."
    assert aa.brighten("A dark hallway, he sits in the dark.") == "A bright hallway, he sits in soft daylight."
    keep = "VERA with dark-chestnut hair in a dark-green cardigan, client in a dark coat."
    assert aa.brighten(keep) == keep
    assert "ЗАПРЕЩЕНЫ тёмные сцены" in aa._SHOTS_PROMPT
    episode, data = _plan_input()
    data["shots"]["memory_1"].update(prompt_en="Dimly lit stairwell, Alina climbs.", setting_en="a dark kitchen at night")
    data["memory_lines"] = [2]
    shots = {s["slot"]: s for s in aa.build_plan(data, episode, key=0, simple=3)["shots"]}
    assert shots["memory_1"]["prompt_en"] == "brightly lit stairwell, Alina climbs."
    assert shots["memory_1"]["setting_en"] == "a bright kitchen in the early evening"
    from agents.scene_agent import _shot_prompt   # кадры из старой раскадровки (возобновление) — тоже
    assert "Dimly" not in _shot_prompt(SERIES, {"client_visual_en": "x"}, {"characters": [], "prompt_en": "Dimly lit hall"})


def test_plan_pieces_snaps_to_frames_and_replaces_frozen_clip_tail():
    from agents.series_assembly import FPS, plan_pieces
    shots = [{"image_path": "a.png"}, {"image_path": "d.png", "video_path": "d.mp4"},
             {"image_path": "l.png", "video_path": "l.mp4", "lipsync": True}]
    # отрезки серии 2 после темпа ×1.15: без привязки к сетке кадров копилось до 0.15 с
    durations = [6.035, 3.113, 6.939, 6.122, 2.226, 5.948, 1.913, 6.243, 4.939, 6.087, 4.835, 3.073]
    timeline = [{"index": 0, "duration": d, "clip": False} for d in durations]
    pieces = plan_pieces(timeline, shots, max_still=3.0)
    for p in pieces:
        assert p["duration"] * FPS == pytest.approx(round(p["duration"] * FPS))   # целое число кадров
    assert sum(p["duration"] for p in pieces) == pytest.approx(sum(durations), abs=0.5 / FPS)
    edges = list(accumulate(p["duration"] for p in pieces))
    for seg_end in accumulate(durations):                          # каждая смена кадра — не дальше полукадра
        assert min(abs(e - seg_end) for e in edges) <= 0.5 / FPS + 1e-9
    # анимация 5 с на 7.98-секундном отрезке: 6 с клипа с замедлением, остаток — снимок с наездом, не стоп-кадр
    pieces = plan_pieces([{"index": 1, "duration": 7.98, "clip": True}], shots, max_still=3.0, clip_seconds={1: 5.0})
    assert [(p["clip"], p["punch"]) for p in pieces] == [(True, False), (False, True)]
    assert pieces[0]["duration"] == pytest.approx(6.0) and pieces[1]["duration"] == pytest.approx(1.98, abs=1 / FPS)
    # хвост короче секунды, липсинк и монтаж без max_still — один кусок клипа, как раньше
    for seg, kw in (({"index": 1, "duration": 6.5, "clip": True}, {"max_still": 3.0}),
                    ({"index": 2, "duration": 9.0, "clip": True}, {"max_still": 3.0}),
                    ({"index": 1, "duration": 9.0, "clip": True}, {})):
        assert [p["clip"] for p in plan_pieces([seg], shots, **kw)] == [True]


def test_lipsync_first_line_audio_starts_where_its_shot_starts(tmp_path):
    """Кадр первой реплики в монтаже — с 0.0; если озвучка начинается позже, звук клипа режется тоже с 0,
    иначе губы опережают голос на start первой реплики."""
    from agents import lipsync_agent as ls
    lines = [{"role": "ЛЕВ", "text": "Скрипку я продал.", "start": 0.4, "end": 2.0},
             {"role": "ВЕРА", "text": "Зачем?", "start": 2.3, "end": 3.0},
             {"role": "ЛЕВ", "text": "Не слышу.", "start": 3.3, "end": 5.0}]
    plan = {"turn_line": 2, "assignment": [{"slot": "client_open", "shot": 0}, {"slot": "vera_ask", "shot": 1},
                                           {"slot": "client_turn", "shot": 2}]}
    shots = [{"image_url": f"u{i}"} for i in range(3)]
    cuts = []
    kie = MagicMock(upload_file=lambda *a: "url", download=lambda url, dest: dest,
                    run_task=lambda *a, **k: {"urls": ["v"], "credits": 1})
    with patch.object(ls, "_cut_audio", side_effect=lambda src, s, e, dest: cuts.append((s, e)) or dest), \
         patch.dict("sys.modules", {"utils.kie_client": kie}), patch("utils.kie_client", kie, create=True):
        clips = ls.render(SERIES, plan, shots, {"audio_path": "d.wav", "lines": lines}, tmp_path)
    assert sorted(clips) == [0, 2] and sorted(cuts) == [(0.0, 2.0), (3.3, 5.0)]


def test_punch_keeps_face_height_out_of_subtitles(tmp_path):
    """Наезд держит лицо на высоте FACE_Y, а не в центре: иначе субтитры закрывают рот (серия 2)."""
    import subprocess
    from agents.series_assembly import FACE_Y, _render_still
    w, h = 108, 192
    img, out = tmp_path / "face.png", tmp_path / "punch.mp4"
    y0 = int(h * FACE_Y)                       # белая полоса «лица» на высоте FACE_Y
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=black:s={w}x{h}", "-vf",
                    f"drawbox=x=0:y={y0 - 2}:w={w}:h=4:color=white:t=fill", "-frames:v", "1", str(img)], check=True)
    _render_still(str(img), 0.2, str(out), zoom_in=True, size=(w, h), punch=True)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    rows = np.frombuffer(raw, np.uint8).reshape(h, w).mean(axis=1)
    assert abs(int(rows.argmax()) - y0) <= 3, (int(rows.argmax()), y0)

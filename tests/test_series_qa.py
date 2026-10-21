"""Series QA: каждая проверка ловит свою поломку и молчит на целом эпизоде (синтетический звук, без сети)."""

import numpy as np

from agents import series_qa as q

SR = q.SR


def _speechy(seconds: float, seed: int = 0) -> np.ndarray:
    """Шум «словами»: всплески 0.2-0.6 с с паузами — огибающая как у речи."""
    rng = np.random.default_rng(seed)
    out, t = [], 0.0
    while t < seconds:
        word, gap = rng.uniform(0.2, 0.6), rng.uniform(0.05, 0.4)
        out += [rng.normal(0, rng.uniform(0.1, 0.3), int(word * SR)), np.zeros(int(gap * SR))]
        t += word + gap
    return np.concatenate(out)[:int(seconds * SR)].astype(np.float32)


def test_sync_passes_on_mix_and_catches_cut_audio():
    voice = _speechy(30)
    music = np.random.default_rng(9).normal(0, 0.01, len(voice)).astype(np.float32)
    assert q.check_sync(voice * 0.8 + music, voice)[0] == []

    cut = 10 * SR  # ffmpeg вырезал 3 с посередине, длина добита тишиной — как у Бублика
    broken = np.concatenate([voice[:cut], voice[cut + 3 * SR:], np.zeros(3 * SR, np.float32)])
    problems, metrics = q.check_sync(broken, voice)
    assert any("-3.00" in p for p in problems)
    assert 0.0 in metrics["sync_lags"]


def _tone(seconds: float, f0: float, bright: bool, seed: int) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    harmonics = range(1, 12) if bright else range(1, 4)
    sig = sum(np.sin(2 * np.pi * f0 * k * t) / (k if bright else k ** 2) for k in harmonics)
    return (sig * 0.2 + np.random.default_rng(seed).normal(0, 0.01, len(t))).astype(np.float32)


def test_speakers_flag_line_in_other_voice():
    roles = ["АРТЁМ", "ВЕРА"] * 4
    voices = {"АРТЁМ": (110, True), "ВЕРА": (230, False)}
    pieces, lines, t = [], [], 0.0
    for i, role in enumerate(roles):
        f0, bright = voices[role]
        seg = _tone(1.5, f0 * (1 + 0.03 * (i % 3)), bright, i)
        pieces += [seg, np.zeros(SR // 4, np.float32)]
        lines.append({"role": role, "text": "слово " * 4, "start": t, "end": t + 1.5})
        t += 1.75
    audio = np.concatenate(pieces)
    assert q.check_speakers(audio, lines)[0] == []

    lines[3] = {**lines[3], "role": "АРТЁМ"}  # реплику Веры записали голосом Артёма
    problems = q.check_speakers(audio, lines)[0]
    assert len(problems) == 1 and problems[0].startswith("реплика 4 (АРТЁМ) звучит голосом ВЕРА")


def test_speakers_skip_when_role_has_single_line():
    lines = [{"role": "А", "text": "x", "start": 0, "end": 1}, {"role": "Б", "text": "x", "start": 1, "end": 2},
             {"role": "А", "text": "x", "start": 2, "end": 3}]
    assert q.check_speakers(_speechy(3), lines) == ([], {})


def _ass(path, events):
    rows = ["[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for s, e in events:
        fmt = lambda x: f"0:{int(x // 60):02d}:{x % 60:05.2f}"
        rows.append(f"Dialogue: 0,{fmt(s)},{fmt(e)},Default,,0,0,620,,СЛОВО")
        rows.append(f"Dialogue: 2,{fmt(s)},{fmt(e)},Speaker,,0,0,752,,ВЕРА")
    path.write_text("\n".join(rows), encoding="utf-8")
    return str(path)


def test_subtitles_cover_lines_and_end_inside_video(tmp_path):
    lines = [{"start": 0.0, "end": 2.0}, {"start": 2.5, "end": 5.0}, {"start": 70.0, "end": 72.0}]
    good = _ass(tmp_path / "a.ass", [(0.0, 1.0), (1.0, 2.0), (2.5, 5.0), (70.0, 72.0)])
    assert q.check_subtitles(good, lines, 73.0)[0] == []

    bad = _ass(tmp_path / "b.ass", [(0.0, 2.0), (10.0, 12.0), (70.0, 74.0)])
    problems = q.check_subtitles(bad, lines, 73.0)[0]
    assert "у реплики 2 нет субтитров" in problems
    assert any("вне реплик" in p for p in problems) and "субтитры идут после конца ролика" in problems


def test_check_reports_problems_by_category_and_survives_broken_check(monkeypatch):
    monkeypatch.setattr(q, "_pcm", lambda path, *a: np.zeros(SR, np.float32))
    monkeypatch.setattr(q, "check_sync", lambda *a: ([], {"sync_lags": [0.0]}))
    monkeypatch.setattr(q, "check_speech", lambda *a: (["реплика 2 (ВЕРА) не слышна"], {}))
    monkeypatch.setattr(q, "check_speakers", lambda *a: (_ for _ in ()).throw(ValueError("librosa")))
    monkeypatch.setattr(q, "check_subtitles", lambda *a: ([], {}))
    monkeypatch.setattr(q, "check_sound", lambda *a: ([], {"lufs": -16.0}))
    monkeypatch.setattr(q, "check_video", lambda *a: ([], {}))
    r = q.check("v.mp4", {"audio_path": "d.wav", "lines": []}, "s.ass")
    assert r["passed"] is False
    assert r["problems"] == [{"category": "speech", "text": "реплика 2 (ВЕРА) не слышна"}]
    assert "speaker_error" in r["metrics"] and r["metrics"]["lufs"] == -16.0
    assert {p["category"] for p in r["problems"]} <= q.VOICE_CATEGORIES  # такой брак лечится переозвучкой

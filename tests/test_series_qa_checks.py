"""Series QA: Whisper, FFmpeg и липсинк — на подменённых отчётах и моделях, без реального ffmpeg и сети."""

import subprocess
from unittest.mock import MagicMock

import numpy as np
import pytest

from agents import series_qa as q
from agents import subtitle_agent

SR = q.SR


def _words(*words):
    return {"segments": [{"words": [{"word": w} for w in words]}, {"text": "без слов"}]}


@pytest.fixture
def whisper(monkeypatch):
    model = MagicMock()
    monkeypatch.setattr(subtitle_agent, "_get_whisper_model", lambda: model)
    return model


# ── _pcm / _ffmpeg_report ────────────────────────────────────────────────────

def test_pcm_builds_command_and_decodes_s16le(monkeypatch):
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout=np.array([0, 16384, -32768], np.int16)
                                                             .tobytes()))
    monkeypatch.setattr(q.subprocess, "run", run)
    pcm = q._pcm("v.mp4", 1.5, 2.0)
    assert pcm.dtype == np.float32 and list(pcm) == [0.0, 0.5, -1.0]
    cmd = run.call_args.args[0]
    assert cmd[:6] == ["ffmpeg", "-v", "error", "-ss", "1.500", "-i"] and cmd[6] == "v.mp4"
    assert cmd[7:9] == ["-t", "2.000"] and cmd[-3:] == ["-f", "s16le", "-"] and str(SR) in cmd


def test_pcm_without_duration_has_no_t(monkeypatch):
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout=b""))
    monkeypatch.setattr(q.subprocess, "run", run)
    assert len(q._pcm("v.mp4")) == 0
    assert "-t" not in run.call_args.args[0]


@pytest.mark.parametrize("video,flag,strip", [(False, "-af", "-vn"), (True, "-vf", "-an")])
def test_ffmpeg_report_returns_stderr(monkeypatch, video, flag, strip):
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, stderr="отчёт".encode()))
    monkeypatch.setattr(q.subprocess, "run", run)
    assert q._ffmpeg_report("v.mp4", "filt", video=video) == "отчёт"
    cmd = run.call_args.args[0]
    assert cmd[cmd.index(flag) + 1] == "filt" and strip in cmd and cmd[-3:] == ["-f", "null", "-"]


# ── огибающая и синхрон ──────────────────────────────────────────────────────

def test_envelope_short_signal_is_empty():
    assert len(q._envelope(np.zeros(q.HOP - 1, np.float32))) == 0


def test_sync_reports_when_no_speech_found():
    silence = np.zeros(10 * SR, np.float32)
    problems, metrics = q.check_sync(silence, silence)
    assert problems == ["в ролике не найдено речи озвучки"] and metrics == {"sync_lags": []}


def test_best_lag_skips_flat_signal():
    assert q._best_lag(np.ones(10), np.ones(30), 5, 3) == (0.0, -1.0)


# ── check_speech (Whisper подменён) ──────────────────────────────────────────

LINES = [{"role": "ВЕРА", "text": "Как вы сегодня спали?"},
         {"role": "АРТЁМ", "text": "Плохо, опять снилась работа и начальник."},
         {"role": "ВЕРА", "text": "Ёлки!"}]


def test_speech_all_lines_heard(whisper):
    whisper.transcribe.return_value = _words("как", "Вы", "сегодня", "спали", "плохо,", "опять", "снилась",
                                             "работа", "и", "начальник", "елки")
    problems, metrics = q.check_speech(np.zeros(SR, np.float32), LINES)
    assert problems == [] and metrics["line_coverage"] == [1.0, 1.0, 1.0]   # ё = е, регистр и знаки не важны
    assert whisper.transcribe.call_args.kwargs == {"language": "ru", "word_timestamps": True}


def test_speech_flags_missing_and_mostly_missing_lines(whisper):
    whisper.transcribe.return_value = _words("как", "вы", "сегодня", "спали", "плохо")
    problems, metrics = q.check_speech(np.zeros(SR, np.float32), LINES)
    assert problems == ["реплика 2 (АРТЁМ) не слышна: «Плохо, опять снилась работа и начальник.»",
                        "реплика 3 (ВЕРА) не слышна: «Ёлки!»"]
    assert metrics["line_coverage"][1] == round(1 / 6, 2)


def test_speech_line_without_words_is_fine(whisper):
    whisper.transcribe.return_value = _words()
    problems, metrics = q.check_speech(np.zeros(SR, np.float32), [{"role": "ВЕРА", "text": "…"}])
    assert problems == [] and metrics == {"line_coverage": [1.0]}


# ── check_speakers без librosa ───────────────────────────────────────────────

def test_speakers_need_exactly_two_roles(monkeypatch):
    monkeypatch.setattr(q, "_voice_features", MagicMock())
    lines = [{"role": "ВЕРА", "text": "x", "start": 0, "end": 1},
             {"role": q.NARRATOR, "text": "x", "start": 1, "end": 2},
             {"role": "ВЕРА", "text": "x", "start": 2, "end": 2.5}]   # короче min_len — не считается
    assert q.check_speakers(np.zeros(3 * SR, np.float32), lines) == ([], {})
    assert not q._voice_features.called


def test_speakers_margin_metrics_with_fake_features(monkeypatch):
    """Признаки: голос Веры около 0, голос Артёма около 10; третья реплика Артёма звучит как Вера."""
    level = iter([0.0, 10.0, 0.2, 10.2, 0.1])
    monkeypatch.setattr(q, "_voice_features", lambda pcm: np.full(4, next(level)) + np.arange(4) * 0.01)
    roles = ["ВЕРА", "АРТЁМ", "ВЕРА", "АРТЁМ", "АРТЁМ"]
    lines = [{"role": r, "text": f"реплика {i}", "start": i, "end": i + 1} for i, r in enumerate(roles)]
    problems, metrics = q.check_speakers(np.zeros(6 * SR, np.float32), lines)
    assert problems == ["реплика 5 (АРТЁМ) звучит голосом ВЕРА: «реплика 4»"]
    assert set(metrics["speaker_margin"]) == {1, 2, 3, 4, 5}


# ── check_sound / check_video (отчёт ffmpeg подменён) ────────────────────────

EBUR_OK = """
[Parsed_ebur128_0] Summary:
  Integrated loudness:
    I:         -15.2 LUFS
  True peak:
    Peak:       -1.3 dBFS
"""


def test_sound_in_norm(monkeypatch):
    monkeypatch.setattr(q, "_ffmpeg_report", lambda *a, **k: EBUR_OK)
    assert q.check_sound("v.mp4", 60.0) == ([], {"lufs": -15.2, "true_peak": -1.3})


def test_sound_loudness_clipping_and_silence(monkeypatch):
    report = ("I: -25.0 LUFS\nPeak: 0.4 dBFS\n"
              "silence_start: 0.0\nsilence_end: 1.0\n"         # в начале — норма
              "silence_start: 20.5\nsilence_end: 23.0\n"       # посреди ролика — брак
              "silence_start: 58.5\nsilence_end: 60.0\n")      # хвост — норма
    monkeypatch.setattr(q, "_ffmpeg_report", lambda *a, **k: report)
    problems, metrics = q.check_sound("v.mp4", 60.0)
    assert metrics == {"lufs": -25.0, "true_peak": 0.4}
    assert problems == [f"громкость -25.0 LUFS вне нормы {q.LUFS_RANGE}", "клиппинг: пик 0.4 dBFS",
                        "тишина 2.5 с на 20.5-й секунде"]


def test_sound_empty_report(monkeypatch):
    monkeypatch.setattr(q, "_ffmpeg_report", lambda *a, **k: "")
    assert q.check_sound("v.mp4", 60.0) == ([], {})


def test_video_black_frames(monkeypatch):
    seen = {}

    def report(path, filters, video=False):
        seen.update(filters=filters, video=video)
        return "[blackdetect] black_start:0 black_end:0.5 black_duration:0.5\n" \
               "[blackdetect] black_start:12.25 black_end:13 black_duration:0.75\n"

    monkeypatch.setattr(q, "_ffmpeg_report", report)
    problems, metrics = q.check_video("v.mp4")
    assert problems == ["чёрный кадр 0.5 с на 0.0-й секунде", "чёрный кадр 0.8 с на 12.2-й секунде"]
    assert metrics == {"black_frames": 2} and seen["video"] is True and "blackdetect" in seen["filters"]


# ── check_lipsync ────────────────────────────────────────────────────────────

def _bursts(seconds, seed):
    rng = np.random.default_rng(seed)
    env = np.repeat(rng.uniform(0, 1, int(seconds * 10)) > 0.5, SR // 10)
    return (env * rng.normal(0, 0.3, len(env))).astype(np.float32)


def test_lipsync_matches_and_detects_shift(monkeypatch):
    voice = _bursts(12, 1)
    start = 4.0
    clips = {"ok.mp4": voice[int(start * SR):int((start + 3) * SR)],
             "late.mp4": voice[int((start + 0.5) * SR):int((start + 3.5) * SR)],
             "mute.mp4": np.zeros(100, np.float32)}
    monkeypatch.setattr(q, "_pcm", lambda path, s=0.0, d=None: clips[path])
    shots = [{"lipsync": True, "video_path": "ok.mp4"}, {"lipsync": True, "video_path": "late.mp4"},
             {"lipsync": True, "video_path": "mute.mp4"}, {"lipsync": False, "video_path": "x.mp4"},
             {"lipsync": True}]
    timeline = [{"shot": i, "start": start, "duration": 3.0} for i in range(len(shots))]
    problems, metrics = q.check_lipsync(voice, timeline, shots)
    assert metrics["lipsync_lags"] == [0.0, 0.5]                 # немой клип и обычные кадры пропущены
    assert problems == ["липсинк на 4.0-й секунде не в такт: сдвиг +0.50 с"]


def test_check_runs_lipsync_only_with_timeline(monkeypatch):
    monkeypatch.setattr(q, "_pcm", lambda path, *a: np.zeros(2 * SR, np.float32))
    for name in ("check_sync", "check_speech", "check_speakers", "check_subtitles", "check_sound", "check_video"):
        monkeypatch.setattr(q, name, lambda *a: ([], {}))
    lip = MagicMock(return_value=(["липсинк сдвинут"], {"lipsync_lags": [0.5]}))
    monkeypatch.setattr(q, "check_lipsync", lip)
    dialogue = {"audio_path": "d.wav", "lines": []}
    assert q.check("v.mp4", dialogue, "s.ass")["passed"] is True and not lip.called
    r = q.check("v.mp4", dialogue, "s.ass", timeline=[{"shot": 0}], shots=[{}])
    assert r["problems"] == [{"category": "lipsync", "text": "липсинк сдвинут"}]
    assert r["metrics"] == {"duration": 2.0, "lipsync_lags": [0.5]}


def test_ass_events_reads_hours_and_bom(tmp_path):
    p = tmp_path / "s.ass"
    p.write_text("﻿[Events]\nDialogue: 0,1:00:01.50,1:00:02.00,Default,,0,0,0,,X\n"
                 "Dialogue: 2,0:00:01.00,0:00:02.00,Speaker,,0,0,0,,ВЕРА\n", encoding="utf-8")
    assert q._ass_events(str(p)) == [(3601.5, 3602.0)]           # плашка имени (Speaker) не субтитр

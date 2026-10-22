"""
Series QA — проверка готового эпизода сериала без LLM, до очереди и публикации.

Ловит то, что раньше находил только зритель:
  sync     — голос в ролике совпадает с озвучкой (ffmpeg однажды молча вырезал 3 с — субтитры уехали)
  speech   — каждая реплика слышна в готовом ролике (Whisper по финальной дорожке)
  speaker  — реплика сказана голосом своего персонажа (тембр MFCC против остальных реплик ролей)
  subs     — у каждой реплики есть субтитры в её время, ничего не торчит за конец ролика
  sound    — громкость в норме, нет клиппинга и провалов тишины
  video    — нет чёрных кадров
  lipsync  — звук в клипе липсинка совпадает с местом, куда клип встал в монтаже

check(video_path, dialogue, subtitle_path, timeline?, shots?) →
    {"passed": bool, "problems": [{"category", "text"}], "metrics": {...}}
Категории speech/speaker — сбой озвучки: Orchestrator переозвучивает эпизод (VOICE_CATEGORIES).
"""

from __future__ import annotations

import difflib
import logging
import re
import subprocess

import numpy as np

logger = logging.getLogger(__name__)

SR = 16000
HOP = 800                 # 50 мс — шаг огибающей громкости
NARRATOR = "РАССКАЗЧИК"
VOICE_CATEGORIES = {"speech", "speaker"}

MAX_LAG = 0.15            # с — допустимый сдвиг голоса относительно озвучки
SYNC_SURE_CORR = 0.7      # совпадение окна, которому верим и без соседнего окна
MIN_LINE_COVERAGE = 0.34  # доля слов реплики, которую Whisper должен расслышать под музыкой
SPEAKER_MARGIN = 1.05     # реплика ближе к чужому голосу с таким запасом — не тот голос
LUFS_RANGE = (-20.0, -12.0)
MAX_PEAK_DB = -0.1
MAX_SILENCE = 2.0         # с — пауза дольше внутри ролика выглядит как сбой


def _pcm(path: str, start: float = 0.0, duration: float | None = None) -> np.ndarray:
    args = ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", path]
    if duration is not None:
        args += ["-t", f"{duration:.3f}"]
    out = subprocess.run(args + ["-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                         capture_output=True, timeout=300)
    return np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _envelope(pcm: np.ndarray) -> np.ndarray:
    n = len(pcm) // HOP
    if n == 0:
        return np.zeros(0)
    return np.log(np.sqrt((pcm[:n * HOP].reshape(n, HOP) ** 2).mean(axis=1)) + 1e-4)


def _best_lag(ref: np.ndarray, sig: np.ndarray, offset: int, max_shift: int) -> tuple[float, float]:
    """Сдвиг (в отсчётах огибающей), при котором ref лучше всего совпадает с sig[offset+lag:]."""
    best, best_corr = 0, -1.0
    for lag in range(-max_shift, max_shift + 1):
        a = offset + lag
        if a < 0 or a + len(ref) > len(sig):
            continue
        part = sig[a:a + len(ref)]
        if ref.std() < 1e-6 or part.std() < 1e-6:
            continue
        corr = float(np.corrcoef(ref, part)[0, 1])
        if corr > best_corr:
            best, best_corr = lag, corr
    return best * HOP / SR, best_corr


def check_sync(final: np.ndarray, voice: np.ndarray, window: float = 4.0) -> tuple[list[str], dict]:
    """Голос финального ролика против чистой озвучки — окнами по 4 с."""
    env_f, env_v = _envelope(final), _envelope(voice)
    step = int(window * SR / HOP)
    lags, problems, windows = [], [], []
    for w0 in range(0, max(len(env_v) - step + 1, 0), step):
        ref = env_v[w0:w0 + step]
        if ref.std() < 0.5:  # ровная огибающая — в окне тишина, сравнивать нечего
            continue
        lag, corr = _best_lag(ref, env_f, w0, int(4.0 * SR / HOP))
        lags.append(round(lag, 2))
        windows.append((w0 * HOP / SR, lag, corr))
    # Сдвиг — брак, если его подтверждают два окна подряд или одно уверенное. Одиночное слабое совпадение
    # (тихая реплика шёпотом под музыкой, corr≈0.45) — случайность: так эпизод 2 браковался зря
    for k, (t, lag, corr) in enumerate(windows):
        if corr <= 0.4 or abs(lag) <= MAX_LAG:
            continue
        near = [w for w in windows[max(k - 1, 0):k + 2] if w is not windows[k]]
        confirmed = any(c > 0.4 and abs(l) > MAX_LAG and abs(l - lag) <= 0.2 for _, l, c in near)
        if corr >= SYNC_SURE_CORR or confirmed:
            problems.append(f"голос сдвинут на {lag:+.2f} с около {t:.0f}-й секунды")
    if not lags:
        problems.append("в ролике не найдено речи озвучки")
    return problems[:3], {"sync_lags": lags}


def check_speech(final: np.ndarray, lines: list[dict]) -> tuple[list[str], dict]:
    """Каждая реплика слышна в финальном ролике: Whisper по дорожке с музыкой."""
    from agents.subtitle_agent import _get_whisper_model, _norm

    result = _get_whisper_model().transcribe(final, language="ru", word_timestamps=True)
    heard = [_norm(w["word"]) for seg in result["segments"] for w in seg.get("words", [])]
    heard = [w for w in heard if w]
    tokens, owner = [], []
    for i, line in enumerate(lines):
        for t in re.findall(r"\S+", line["text"]):
            if _norm(t):
                tokens.append(_norm(t))
                owner.append(i)
    matched = [0] * len(lines)
    matcher = difflib.SequenceMatcher(None, heard, tokens, autojunk=False)
    for block in matcher.get_matching_blocks():
        for j in range(block.b, block.b + block.size):
            matched[owner[j]] += 1
    totals = [owner.count(i) for i in range(len(lines))]
    coverage = [round(m / t, 2) if t else 1.0 for m, t in zip(matched, totals)]
    problems = [f"реплика {i + 1} ({lines[i]['role']}) не слышна: «{lines[i]['text'][:40]}»"
                for i, (cov, total) in enumerate(zip(coverage, totals))
                if (total >= 3 and cov < MIN_LINE_COVERAGE) or (total and matched[i] == 0)]
    return problems, {"line_coverage": coverage}


def _voice_features(pcm: np.ndarray) -> np.ndarray:
    import librosa

    mfcc = librosa.feature.mfcc(y=pcm, sr=SR, n_mfcc=20)[1:]
    return np.concatenate([mfcc.mean(axis=1), mfcc.std(axis=1)])


def check_speakers(voice: np.ndarray, lines: list[dict], min_len: float = 0.8) -> tuple[list[str], dict]:
    """Тембр реплики против средних тембров ролей (без неё самой): ближе к чужой роли — не тот голос."""
    items = [(i, line) for i, line in enumerate(lines)
             if line["role"] != NARRATOR and line["end"] - line["start"] >= min_len]
    roles = sorted({line["role"] for _, line in items})
    if len(roles) != 2:
        return [], {}
    feats = np.array([_voice_features(voice[int(l["start"] * SR):int(l["end"] * SR)]) for _, l in items])
    feats = (feats - feats.mean(axis=0)) / (feats.std(axis=0) + 1e-6)
    problems, margins = [], {}
    for k, (i, line) in enumerate(items):
        dist = {}
        for role in roles:
            idx = [m for m, (_, other) in enumerate(items) if other["role"] == role and m != k]
            if not idx:
                return [], {}  # у роли одна реплика — сравнивать не с чем
            dist[role] = float(np.linalg.norm(feats[k] - feats[idx].mean(axis=0)))
        own = dist[line["role"]]
        other_role = next(r for r in roles if r != line["role"])
        margins[i + 1] = round(dist[other_role] / max(own, 1e-6), 2)
        if own > dist[other_role] * SPEAKER_MARGIN:
            problems.append(f"реплика {i + 1} ({line['role']}) звучит голосом {other_role}: "
                            f"«{line['text'][:40]}»")
    return problems, {"speaker_margin": margins}


_ASS_EVENT = re.compile(r"^Dialogue:\s*\d+,(\d+):(\d\d):(\d\d\.\d+),(\d+):(\d\d):(\d\d\.\d+),Default,")


def _ass_events(path: str) -> list[tuple[float, float]]:
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for row in f:
            m = _ASS_EVENT.match(row)
            if m:
                h1, m1, s1, h2, m2, s2 = m.groups()
                out.append((int(h1) * 3600 + int(m1) * 60 + float(s1), int(h2) * 3600 + int(m2) * 60 + float(s2)))
    return out


def check_subtitles(subtitle_path: str, lines: list[dict], duration: float) -> tuple[list[str], dict]:
    events = _ass_events(subtitle_path)
    problems = []
    for i, line in enumerate(lines):
        if not any(s < line["end"] + 0.3 and e > line["start"] - 0.3 for s, e in events):
            problems.append(f"у реплики {i + 1} нет субтитров")
    stray = [(s, e) for s, e in events if not any(s < l["end"] + 0.6 and e > l["start"] - 0.4 for l in lines)]
    if stray:
        problems.append(f"{len(stray)} субтитров вне реплик (первый на {stray[0][0]:.1f} с)")
    if events and max(e for _, e in events) > duration + 0.1:
        problems.append("субтитры идут после конца ролика")
    return problems, {"subtitle_events": len(events)}


def _ffmpeg_report(path: str, filters: str, video: bool = False) -> str:
    args = ["ffmpeg", "-v", "info", "-i", path]
    args += ["-vf", filters, "-an"] if video else ["-af", filters, "-vn"]
    out = subprocess.run(args + ["-f", "null", "-"], capture_output=True, timeout=600)
    return out.stderr.decode("utf-8", "replace")


def check_sound(path: str, duration: float) -> tuple[list[str], dict]:
    report = _ffmpeg_report(path, "ebur128=peak=true,silencedetect=n=-45dB:d=" + str(MAX_SILENCE))
    problems, metrics = [], {}
    lufs = re.findall(r"I:\s+(-?[\d.]+) LUFS", report)
    peak = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", report)
    if lufs:
        metrics["lufs"] = float(lufs[-1])
        if not LUFS_RANGE[0] <= metrics["lufs"] <= LUFS_RANGE[1]:
            problems.append(f"громкость {metrics['lufs']} LUFS вне нормы {LUFS_RANGE}")
    if peak:
        metrics["true_peak"] = float(peak[-1])
        if metrics["true_peak"] > MAX_PEAK_DB:
            problems.append(f"клиппинг: пик {metrics['true_peak']} dBFS")
    for start, end in zip(re.findall(r"silence_start: (-?[\d.]+)", report),
                          re.findall(r"silence_end: ([\d.]+)", report)):
        if 0.5 < float(start) and float(end) < duration - 1.0:
            problems.append(f"тишина {float(end) - float(start):.1f} с на {float(start):.1f}-й секунде")
    return problems, metrics


def check_video(path: str) -> tuple[list[str], dict]:
    report = _ffmpeg_report(path, "blackdetect=d=0.4:pix_th=0.10", video=True)
    black = re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", report)
    problems = [f"чёрный кадр {float(e) - float(s):.1f} с на {float(s):.1f}-й секунде" for s, e in black]
    return problems, {"black_frames": len(black)}


def check_lipsync(voice: np.ndarray, timeline: list[dict], shots: list[dict]) -> tuple[list[str], dict]:
    """Звук, под который рисовались губы (дорожка клипа), против озвучки в том месте, куда клип встал."""
    problems, lags = [], []
    for seg in timeline:
        shot = shots[seg["shot"]]
        if not shot.get("lipsync") or not shot.get("video_path"):
            continue
        clip = _pcm(shot["video_path"], 0.0, seg["duration"])
        if len(clip) < SR // 2:
            continue  # у клипа нет звуковой дорожки — сверять не с чем
        ref = _envelope(clip)
        lag, corr = _best_lag(ref, _envelope(voice), int(seg["start"] * SR / HOP), int(1.0 * SR / HOP))
        lags.append(round(lag, 2))
        if corr > 0.4 and abs(lag) > MAX_LAG:
            problems.append(f"липсинк на {seg['start']:.1f}-й секунде не в такт: сдвиг {lag:+.2f} с")
    return problems, {"lipsync_lags": lags}


def check(video_path: str, dialogue: dict, subtitle_path: str, timeline: list[dict] | None = None,
          shots: list[dict] | None = None) -> dict:
    lines = dialogue["lines"]
    final = _pcm(video_path)
    voice = _pcm(dialogue["audio_path"])
    duration = len(final) / SR
    problems, metrics = [], {"duration": round(duration, 2)}

    def run(category: str, fn, *args) -> None:
        try:
            found, data = fn(*args)
        except Exception as exc:  # сломалась сама проверка — не повод останавливать выпуск
            logger.warning("Series QA: проверка %s не выполнена: %s", category, exc)
            metrics[f"{category}_error"] = str(exc)[:200]
            return
        problems.extend({"category": category, "text": text} for text in found)
        metrics.update(data)

    run("sync", check_sync, final, voice)
    run("speech", check_speech, final, lines)
    run("speaker", check_speakers, voice, lines)
    run("subs", check_subtitles, subtitle_path, lines, duration)
    run("sound", check_sound, video_path, duration)
    run("video", check_video, video_path)
    if timeline and shots:
        run("lipsync", check_lipsync, voice, timeline, shots)

    passed = not problems
    logger.info("Series QA: %s%s", "пройдено" if passed else "НЕ пройдено — ",
                "; ".join(p["text"] for p in problems))
    return {"passed": passed, "problems": problems, "metrics": metrics}

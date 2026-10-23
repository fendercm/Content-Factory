"""
Dialogue Voice — озвучка эпизода сериала по ролям.

Режим "dialogue" (series["tts_mode"], по умолчанию): реплики Веры и клиента уходят в Gemini TTS
одним многоголосым запросом (multiSpeakerVoiceConfig, до двух говорящих) — модель сама ведёт
ритм разговора, а эпизод тратит 1–2 запроса бесплатной дневной квоты вместо 12–16. Реплика
рассказчика (только последняя) — отдельным запросом. Границы реплик в общей дорожке восстанавливает
Whisper: его слова выравниваются на текст сценария (subtitle_agent._align_to_script). Если
выравнивание не сошлось — эпизод переозвучивается по репликам.

Режим "lines": каждая реплика — отдельный запрос, склейка с паузами, тайминги точные.

Резерв (series["voice_fallback"]): "edge" — весь эпизод через edge-tts, чтобы голос не сменился
посреди серии; "none" — ошибка, и сериал останавливается до платных кадров (голос героини не
должен меняться между сериями). В Gemini TTS уходит только текст реплик: инструкция по стилю
зачитывается вслух.

Темп (series["voice_tempo"], по умолчанию 1.0): готовая дорожка ускоряется atempo без смены высоты,
тайминги реплик и слов пересчитываются.

API:
    synthesize(series, episode, out_dir) → {"audio_path", "lines": [{role, text, start, end}],
                                             "engine", "duration", "tempo"?}
    apply_tempo(dialogue, tempo) → dialogue с ускоренной дорожкой
"""

import asyncio
import base64
import io
import logging
import os
import re
import time
import wave
from pathlib import Path

import numpy as np
import requests

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
PAUSE_AFTER_COLD_OPEN = 0.7
PAUSE_SPEAKER_CHANGE = 0.3
PAUSE_SAME_SPEAKER = 0.15
DIALOGUE_TAKES = 3         # дублей диалога одним запросом, прежде чем озвучивать по репликам
_SILENCE_THRESHOLD = 0.01  # доля от максимума — обрезка тишины по краям реплики
NARRATOR = "РАССКАЗЧИК"
TTS_MODELS = ["gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts",
              "gemini-3.8-flash-lite-tts"]


class _AlignError(RuntimeError):
    """Общая дорожка диалога не разметилась по репликам."""


def _voices(series: dict, episode: dict) -> dict[str, dict]:
    case = episode["case"]
    client_key = "client_female" if case.get("gender") == "female" else "client_male"
    heroine = series["heroine"]
    return {
        heroine["role"]: {"gemini": heroine["gemini_voice"], "edge": heroine["edge_voice"]},
        episode["client_role"]: series["voices"][client_key],
        NARRATOR: series["voices"]["НАРРАТОР"],
    }


def _gemini_tts(text: str, speech_config: dict) -> np.ndarray:
    """Запрос к Gemini TTS → float32 PCM 24 кГц моно. 429 «в минуту» — ждём и повторяем."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY не задан")
    # У каждой модели своя бесплатная дневная квота: старшие превью — резерв того же семейства голосов
    models = [m.strip() for m in os.getenv("GEMINI_TTS_MODEL", ",".join(TTS_MODELS)).split(",") if m.strip()]
    last = "нет моделей"
    for model in models:
        for attempt in range(3):
            try:
                resp = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"X-Goog-Api-Key": api_key}, timeout=180,
                    json={"contents": [{"parts": [{"text": text}]}],
                          "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": speech_config}})
            except requests.RequestException as exc:
                last = f"{model}: {type(exc).__name__}"
                break
            if resp.status_code == 429 and "PerDay" not in resp.text and attempt < 2:
                delay = 20
                m = re.search(r'"retryDelay":\s*"(\d+)', resp.text)
                if m:
                    delay = min(int(m.group(1)) + 1, 60)
                time.sleep(delay)
                continue
            if resp.status_code != 200:
                last = f"{model}: HTTP {resp.status_code} {resp.text[:120]}"
                break
            try:
                part = resp.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
            except (KeyError, IndexError, ValueError):
                last = f"{model}: нет аудио"
                break
            raw = base64.b64decode(part["data"])
            if raw[:4] == b"RIFF":
                with wave.open(io.BytesIO(raw)) as w:
                    rate, frames, channels = w.getframerate(), w.readframes(w.getnframes()), w.getnchannels()
            else:
                rate, frames, channels = SAMPLE_RATE, raw, 1
            pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            if channels > 1:
                pcm = pcm.reshape(-1, channels).mean(axis=1)
            logger.info("Gemini TTS: %s, %.1f с", model, len(pcm) / rate)
            return _resample(pcm, rate)
    raise RuntimeError(f"Gemini TTS: {last}")


def _gemini_pcm(text: str, voice: str) -> np.ndarray:
    """Одна реплика одним голосом."""
    return _gemini_tts(text, {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}})


def _speaker_name(role: str) -> str:
    """Метка говорящего в тексте для многоголосого TTS: «ИГОРЬ» → «Игорь», «ВИКТОР ПАВЛОВИЧ» → «Виктор»."""
    return role.split()[0].capitalize() if role.split() else role


# Действие в пометке («глядя в пол», «теребит ручку») модель читает вслух — в озвучку идёт только голос
_ACTION_WORD = re.compile(r"^\w+(?:[яа]|вшись|ив|ав|ув|[еи]т|[ая]ет|ует|ется|ится)$")


def _voice_cue(emotion: str) -> str:
    """Пометка для TTS: только как звучит голос. «глухо, глядя в пол» → «глухо»;
    «голос срывается» остаётся; «теребит ручку» → пусто."""
    keep = []
    for part in re.split(r"[,;]", emotion or ""):
        words = re.findall(r"[а-яё]+", part.lower())
        if not words:
            continue
        if "голос" in part.lower() or not any(_ACTION_WORD.match(w) and len(w) > 3 for w in words):
            keep.append(part.strip())
    return ", ".join(keep)


def _turns(lines: list[dict]) -> list[list[dict]]:
    """Подряд идущие реплики одного героя — один ход: два хода одного говорящего подряд модель путает —
    отдаёт голос собеседнику и читает пометку эмоции вслух (Бублик, 2026-10-21)."""
    turns: list[list[dict]] = []
    for line in lines:
        if turns and turns[-1][0]["role"] == line["role"]:
            turns[-1].append(line)
        else:
            turns.append([line])
    return turns


def _gemini_dialogue_pcm(lines: list[dict], voices: dict, direction: str = "") -> np.ndarray:
    """Диалог двух ролей одним запросом: «Вера: …\\nИгорь: …» + голос на каждую метку."""
    roles = list(dict.fromkeys(line["role"] for line in lines))
    if len(roles) == 1:
        return _gemini_pcm(" ".join(line["text"] for line in lines), voices[roles[0]]["gemini"])
    if len(roles) > 2:
        raise _AlignError(f"в одном запросе Gemini TTS не больше двух говорящих, а здесь {roles}")
    # Режиссёрское вступление + «[эмоция]» у реплики: модель играет сцену, а не читает (вслух это
    # не звучит — проверено Whisper'ом 2026-10-21). Одиночный голос так не умеет: читает указания вслух
    text = "\n".join(_speaker_name(turn[0]["role"]) + ": " + " ".join(
        (f"[{_voice_cue(line.get('emotion'))}] " if _voice_cue(line.get("emotion")) else "") + line["text"]
        for line in turn)
        for turn in _turns(lines))
    if direction:
        text = f"{direction}\n\n{text}"
    config = {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": [
        {"speaker": _speaker_name(role), "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voices[role]["gemini"]}}}
        for role in roles]}}
    return _gemini_tts(text, config)


def _edge_pcm(text: str, voice: str, tmp_dir: Path) -> np.ndarray:
    import edge_tts
    from utils.ffmpeg import run_ffmpeg

    mp3 = tmp_dir / "_edge_line.mp3"
    wav = tmp_dir / "_edge_line.wav"

    async def _go():
        name, _, pitch = voice.partition("@")  # "ru-RU-SvetlanaNeural@+20Hz" — голос со сдвигом высоты
        await edge_tts.Communicate(text, name, rate="+0%", pitch=pitch or "+0Hz").save(str(mp3))

    for attempt in range(3):  # сервис Bing периодически не отвечает по таймауту
        try:
            asyncio.run(_go())
            break
        except Exception as exc:
            if attempt == 2:
                raise
            logger.warning("Dialogue: edge-tts ошибка (%s) — повтор", str(exc)[:120])
            time.sleep(3 * (attempt + 1))
    run_ffmpeg(["ffmpeg", "-y", "-i", str(mp3), "-ac", "1", "-ar", str(SAMPLE_RATE), str(wav)],
               timeout=120, what="edge mp3→wav")
    with wave.open(str(wav)) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    mp3.unlink(missing_ok=True)
    wav.unlink(missing_ok=True)
    return pcm


def _resample(pcm: np.ndarray, rate: int) -> np.ndarray:
    if rate == SAMPLE_RATE or len(pcm) == 0:
        return pcm
    n = int(len(pcm) * SAMPLE_RATE / rate)
    return np.interp(np.linspace(0, len(pcm), n, endpoint=False), np.arange(len(pcm)), pcm).astype(np.float32)


def _trim(pcm: np.ndarray) -> np.ndarray:
    """Срезает тишину по краям, оставляя 60 мс запаса."""
    if len(pcm) == 0:
        return pcm
    level = np.abs(pcm)
    thr = max(level.max() * _SILENCE_THRESHOLD, 1e-4)
    idx = np.where(level > thr)[0]
    if len(idx) == 0:
        return pcm
    pad = int(0.06 * SAMPLE_RATE)
    return pcm[max(idx[0] - pad, 0): min(idx[-1] + pad, len(pcm))]


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)


def _write_wav(pcm: np.ndarray, path: Path) -> None:
    peak = float(np.abs(pcm).max()) if len(pcm) else 1.0
    audio = (pcm / (peak or 1.0) * 0.89 * 32767).astype(np.int16)  # пик −1 дБ; громкость выровняет loudnorm
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio.tobytes())


def _line_words(pcm: np.ndarray, lines: list[dict], tmp_dir: Path) -> list[list[dict]]:
    """
    Слова каждой реплики с таймингами в общей дорожке: Whisper → выравнивание на текст сценария.
    → [[{word, start, end}, ...] на реплику]; слова — в написании сценария (для субтитров).
    """
    from agents.subtitle_agent import _align_to_script, _get_whisper_model, _norm

    wav = tmp_dir / "_dialogue_align.wav"
    _write_wav(pcm, wav)
    try:
        result = _get_whisper_model().transcribe(str(wav), language="ru", word_timestamps=True)
    finally:
        wav.unlink(missing_ok=True)
    words = [w for seg in result["segments"] for w in seg.get("words", [])]
    counts = [sum(1 for t in re.findall(r"\S+", line["text"]) if _norm(t)) for line in lines]
    extra = _extra_speech(words, lines)
    if extra:
        raise _AlignError(f"в озвучке лишние слова: «{extra}»")
    aligned = _align_to_script(words, " ".join(line["text"] for line in lines))
    if not aligned or len(aligned) != sum(counts) or 0 in counts:
        raise _AlignError("озвучка не совпала со сценарием")

    per_line, k = [], 0
    for c in counts:
        per_line.append([{"word": w["word"], "start": float(w["start"]), "end": float(w["end"])}
                         for w in aligned[k:k + c]])
        k += c
    bounds = [(w[0]["start"], w[-1]["end"]) for w in per_line]
    if any(e < s for s, e in bounds) or any(nxt[0] < cur[0] for cur, nxt in zip(bounds, bounds[1:])):
        raise _AlignError("реплики в дорожке идут не по порядку")
    return per_line


def _extra_speech(words: list[dict], lines: list[dict]) -> str:
    """Слова, которых нет в сценарии, подряд от двух: модель прочитала вслух пометку («шёпотом, со
    слезами») или режиссуру. Одиночные лишние слова — обычные ошибки распознавания, их пропускаем."""
    import difflib

    from agents.subtitle_agent import _norm

    heard = [_norm(w.get("word", "")) for w in words]
    heard = [w for w in heard if w]
    script = [_norm(t) for line in lines for t in re.findall(r"\S+", line["text"]) if _norm(t)]
    matcher = difflib.SequenceMatcher(None, heard, script, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "delete" and i2 - i1 >= 2:
            return " ".join(heard[i1:i2])
        if op == "replace" and (i2 - i1) - (j2 - j1) >= 2:
            return " ".join(heard[i1:i2])
    return ""


def _line_bounds(pcm: np.ndarray, lines: list[dict], tmp_dir: Path) -> list[tuple[float, float]]:
    """Начало и конец каждой реплики в общей дорожке."""
    return [(w[0]["start"], w[-1]["end"]) for w in _line_words(pcm, lines, tmp_dir)]


def _render_dialogue(lines: list[dict], voices: dict, tmp_dir: Path,
                     direction: str = "") -> tuple[np.ndarray, list[dict]]:
    """Диалог одним запросом + рассказчик отдельно; тайминги — по Whisper."""
    spoken = [line for line in lines if line["role"] != NARRATOR]
    narration = [line for line in lines if line["role"] == NARRATOR]
    if narration and lines[-1]["role"] != NARRATOR:
        raise _AlignError("рассказчик посреди диалога — только по репликам")

    for take in range(1, DIALOGUE_TAKES + 1):  # модель иногда сбивается — ещё дубль, а не озвучка по репликам
        pcm = _trim(_gemini_dialogue_pcm(spoken, voices, direction))
        try:
            words = _line_words(pcm, spoken, tmp_dir)
            break
        except _AlignError as exc:
            if take == DIALOGUE_TAKES:
                raise
            logger.warning("Dialogue: дубль %d не принят (%s) — ещё дубль", take, exc)
    bounds = [(w[0]["start"], w[-1]["end"]) for w in words]

    # Пауза после cold open: режем дорожку посередине тишины между первой и второй репликой
    if len(bounds) > 1:
        gap = bounds[1][0] - bounds[0][1]
        extra = max(PAUSE_AFTER_COLD_OPEN - gap, 0.0)
        if extra > 0:
            cut = int((bounds[0][1] + gap / 2) * SAMPLE_RATE)
            pcm = np.concatenate([pcm[:cut], _silence(extra), pcm[cut:]])
            bounds = [bounds[0]] + [(s + extra, e + extra) for s, e in bounds[1:]]
            words = [words[0]] + [[{**w, "start": w["start"] + extra, "end": w["end"] + extra} for w in line_words]
                                  for line_words in words[1:]]

    timed = [{"role": line["role"], "text": line["text"], "start": round(s, 3), "end": round(e, 3),
              "words": [{"word": w["word"], "start": round(w["start"], 3), "end": round(w["end"], 3)} for w in ws]}
             for line, (s, e), ws in zip(spoken, bounds, words)]
    pieces, t = [pcm], len(pcm) / SAMPLE_RATE
    for line in narration:
        voice_pcm = _trim(_gemini_pcm(line["text"], voices[line["role"]]["gemini"]))
        start = t + PAUSE_SPEAKER_CHANGE
        pieces += [_silence(PAUSE_SPEAKER_CHANGE), voice_pcm]
        t = start + len(voice_pcm) / SAMPLE_RATE
        timed.append({"role": line["role"], "text": line["text"], "start": round(start, 3), "end": round(t, 3)})
    return np.concatenate(pieces), timed


def _render_lines(lines: list[dict], voices: dict, engine: str, tmp_dir: Path) -> tuple[np.ndarray, list[dict]]:
    """Каждая реплика отдельно + паузы по правилам; тайминги точные."""
    clips = []
    for line in lines:
        voice = voices[line["role"]][engine]
        pcm = _gemini_pcm(line["text"], voice) if engine == "gemini" else _edge_pcm(line["text"], voice, tmp_dir)
        clips.append(_trim(pcm))

    pieces, timed, t = [], [], 0.0
    for i, (line, pcm) in enumerate(zip(lines, clips)):
        start = t
        pieces.append(pcm)
        t += len(pcm) / SAMPLE_RATE
        timed.append({"role": line["role"], "text": line["text"], "start": round(start, 3), "end": round(t, 3)})
        if i == len(lines) - 1:
            break
        if i == 0:
            pause = PAUSE_AFTER_COLD_OPEN
        elif lines[i + 1]["role"] != line["role"]:
            pause = PAUSE_SPEAKER_CHANGE
        else:
            pause = PAUSE_SAME_SPEAKER
        pieces.append(_silence(pause))
        t += pause
    audio = np.concatenate(pieces) if pieces else np.zeros(1, dtype=np.float32)
    return audio, timed


def synthesize(series: dict, episode: dict, out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = episode["lines"]
    voices = _voices(series, episode)
    mode = series.get("tts_mode", "dialogue")
    fallback = series.get("voice_fallback", "edge")
    use_gemini = bool(os.getenv("GEMINI_API_KEY")) and os.getenv("GEMINI_TTS", "on").lower() != "off"

    audio, timed, engine = None, None, "gemini"
    if use_gemini:
        try:
            if mode == "dialogue":
                try:
                    audio, timed = _render_dialogue(lines, voices, out_dir, series.get("tts_direction", ""))
                    engine = "gemini-dialogue"
                except _AlignError as exc:
                    logger.warning("Dialogue: %s — переозвучиваю по репликам", exc)
                    audio, timed = _render_lines(lines, voices, "gemini", out_dir)
            else:
                audio, timed = _render_lines(lines, voices, "gemini", out_dir)
        except Exception as exc:
            if fallback != "edge":
                raise RuntimeError(f"{str(exc)[:200]} — резерв edge-tts для сериала выключен "
                                   "(voice_fallback=none): эпизод остановлен до платных кадров") from exc
            logger.warning("Dialogue: Gemini TTS не справился (%s) — весь эпизод через edge-tts", str(exc)[:200])
            audio = None
    if audio is None:
        engine = "edge"
        audio, timed = _render_lines(lines, voices, "edge", out_dir)

    duration = len(audio) / SAMPLE_RATE
    audio_path = out_dir / "dialogue.wav"
    _write_wav(audio, audio_path)
    logger.info("Dialogue: %d реплик, %.1f с, движок %s", len(lines), duration, engine)
    dialogue = {"audio_path": str(audio_path), "lines": timed, "engine": engine, "duration": round(duration, 3)}
    return apply_tempo(dialogue, float(series.get("voice_tempo", 1.0)))


def scale_timing(lines: list[dict], tempo: float) -> list[dict]:
    """Тайминги реплик и слов под дорожку, ускоренную в tempo раз."""
    def at(t: float) -> float:
        return round(t / tempo, 3)
    return [{**line, "start": at(line["start"]), "end": at(line["end"]),
             **({"words": [{**w, "start": at(w["start"]), "end": at(w["end"])} for w in line["words"]]}
                if "words" in line else {})}
            for line in lines]


def apply_tempo(dialogue: dict, tempo: float) -> dict:
    """
    Темп речи без смены высоты голоса (atempo). Делается сразу после озвучки, поэтому субтитры, липсинк
    и Series QA получают уже ускоренную дорожку. Gemini TTS в диалоге читает ~2 слова/с — для ленты
    медленно: зрители уходили в середине серии (2026-10-23). tempo=1 — без изменений.
    """
    if abs(tempo - 1.0) < 1e-3:
        return dialogue
    if not 0.5 <= tempo <= 2.0:
        raise ValueError(f"voice_tempo {tempo}: допустимо 0.5–2.0")
    from utils.ffmpeg import run_ffmpeg

    src = Path(dialogue["audio_path"])
    dest = src.with_name(f"{src.stem}_x{tempo:g}.wav")
    run_ffmpeg(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter:a", f"atempo={tempo}",
                "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(dest)], timeout=120, what="voice tempo")
    with wave.open(str(dest), "rb") as w:
        duration = w.getnframes() / w.getframerate()
    logger.info("Dialogue: темп ×%g — %.1f с → %.1f с", tempo, dialogue["duration"], duration)
    return {**dialogue, "audio_path": str(dest), "lines": scale_timing(dialogue["lines"], tempo),
            "duration": round(duration, 3), "tempo": tempo}

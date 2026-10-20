"""
Dialogue Voice — озвучка эпизода сериала по ролям.

Каждая реплика синтезируется отдельно голосом своей роли и склеивается с паузами — так
известны точные тайминги реплик (для субтитров с цветом по ролям и для монтажа кадров).

Движок выбирается на эпизод целиком: Gemini TTS (бесплатный tier); если хоть одна реплика
не синтезировалась — весь эпизод переозвучивается edge-tts, чтобы голос не сменился посреди серии.
В Gemini TTS уходит только текст реплики: инструкция по стилю зачитывается вслух.

API:
    synthesize(series, episode, out_dir) → {"audio_path", "lines": [{role, text, start, end}],
                                             "engine", "duration"}
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
_SILENCE_THRESHOLD = 0.01  # доля от максимума — обрезка тишины по краям реплики


def _voices(series: dict, episode: dict) -> dict[str, dict]:
    case = episode["case"]
    client_key = "client_female" if case.get("gender") == "female" else "client_male"
    heroine = series["heroine"]
    return {
        "ВЕРА": {"gemini": heroine["gemini_voice"], "edge": heroine["edge_voice"]},
        episode["client_role"]: series["voices"][client_key],
        "РАССКАЗЧИК": series["voices"]["НАРРАТОР"],
    }


def _gemini_pcm(text: str, voice: str) -> np.ndarray:
    """Одна реплика → float32 PCM 24 кГц моно. 429 «в минуту» — ждём и повторяем."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY не задан")
    models = [m.strip() for m in os.getenv("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts,gemini-3.8-flash-lite-tts").split(",") if m.strip()]
    last = "нет моделей"
    for model in models:
        for attempt in range(3):
            try:
                resp = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"X-Goog-Api-Key": api_key}, timeout=90,
                    json={"contents": [{"parts": [{"text": text}]}],
                          "generationConfig": {"responseModalities": ["AUDIO"],
                                               "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}})
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
            return _resample(pcm, rate)
    raise RuntimeError(f"Gemini TTS: {last}")


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


def _render(lines: list[dict], voices: dict, engine: str, tmp_dir: Path) -> list[np.ndarray]:
    out = []
    for line in lines:
        voice = voices[line["role"]][engine]
        pcm = _gemini_pcm(line["text"], voice) if engine == "gemini" else _edge_pcm(line["text"], voice, tmp_dir)
        out.append(_trim(pcm))
    return out


def synthesize(series: dict, episode: dict, out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = episode["lines"]
    voices = _voices(series, episode)

    engine = "gemini" if os.getenv("GEMINI_API_KEY") and os.getenv("GEMINI_TTS", "on").lower() != "off" else "edge"
    try:
        clips = _render(lines, voices, engine, out_dir)
    except Exception as exc:
        if engine != "gemini":
            raise
        logger.warning("Dialogue: Gemini TTS не справился (%s) — весь эпизод через edge-tts", str(exc)[:200])
        engine = "edge"
        clips = _render(lines, voices, engine, out_dir)

    # Склейка с паузами + тайминги
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
        pieces.append(np.zeros(int(pause * SAMPLE_RATE), dtype=np.float32))
        t += pause

    audio = np.concatenate(pieces) if pieces else np.zeros(1, dtype=np.float32)
    peak = float(np.abs(audio).max()) or 1.0
    audio = (audio / peak * 0.89 * 32767).astype(np.int16)  # пик −1 дБ; громкость выровняет loudnorm в сборке
    audio_path = out_dir / "dialogue.wav"
    with wave.open(str(audio_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio.tobytes())

    logger.info("Dialogue: %d реплик, %.1f с, движок %s", len(lines), t, engine)
    return {"audio_path": str(audio_path), "lines": timed, "engine": engine, "duration": round(t, 3)}

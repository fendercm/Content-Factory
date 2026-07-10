"""
Voice Agent — синтез речи из текста сценария.

Приоритет:
  0. ElevenLabs via Kie.ai (если KIE_API_KEY задан — лучшее качество)
  1. CosyVoice v2 via DashScope/Qwen (если DASHSCOPE_API_KEY — бесплатно 500K симв/мес)
  2. Yandex SpeechKit (если YANDEX_API_KEY задан)
  3. Silero TTS V5 (локально, CPU, бесплатно)
  4. edge-tts (всегда доступен, резерв)

Вход:  full_script (str), platform (str), voice (str, опционально)
Выход: {"audio_path": "...", "voice": "...", "platform": "...", "engine": "..."}
"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from typing import Literal

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logger = logging.getLogger(__name__)

Platform = Literal["tiktok", "youtube", "instagram", "linkedin", "twitter"]

KIE_API_KEY = os.getenv("KIE_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")

# Rachel (женский) или Antoni (мужской) — eleven_multilingual_v2 поддерживает русский
ELEVENLABS_VOICES = {
    "male":   {"id": "ErXwobaYiN019PkySvjV", "name": "Antoni"},
    "female": {"id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel"},
}

# CosyVoice-v2 via DashScope — multilingual voices, поддерживают русский
COSYVOICE_VOICES = {
    "male":   "longfei",        # уверенный мужской, multilingual
    "female": "longxiaochun_v2",  # мягкий женский, multilingual
}

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output", "audio")

# Настройки под платформу — rate=+0% чтобы речь звучала естественно, не роботизированно
PLATFORM_SETTINGS = {
    "tiktok":    {"rate": "+0%", "gender": "female", "yandex_voice": "alena", "edge_voice": "ru-RU-SvetlanaNeural"},
    "youtube":   {"rate": "+0%", "gender": "female", "yandex_voice": "alena", "edge_voice": "ru-RU-SvetlanaNeural"},
    "instagram": {"rate": "+0%", "gender": "female", "yandex_voice": "alena", "edge_voice": "ru-RU-SvetlanaNeural"},
    "linkedin":  {"rate": "+0%", "gender": "female", "yandex_voice": "alena", "edge_voice": "ru-RU-SvetlanaNeural"},
    "twitter":   {"rate": "+0%", "gender": "female", "yandex_voice": "alena", "edge_voice": "ru-RU-SvetlanaNeural"},
}

# Silero: спикер по полу — baya (женский, эмоциональный), aidar (мужской)
SILERO_SPEAKER = {"male": "aidar", "female": "baya"}
SILERO_SAMPLE_RATE = 48000

# Yandex-персона → edge-tts голос: в budget mode персоны ниш продолжают работать
# (Svetlana — мягкий женский, Dariya — энергичный женский, Dmitry — мужской)
EDGE_PERSONA_MAP: dict[str, str] = {
    "alena":  "ru-RU-SvetlanaNeural",
    "jane":   "ru-RU-DariyaNeural",
    "lea":    "ru-RU-DariyaNeural",
    "filipp": "ru-RU-DmitryNeural",
    "ermil":  "ru-RU-DmitryNeural",
}

# Per-niche personas — Yandex SpeechKit голоса под характер ниши.
# alena    — мягкий женский, эмпатия (психология, отношения)
# filipp   — уверенный мужской (бизнес, финансы, карьера, продуктивность)
# jane     — энергичный женский (здоровье, мотивация, спорт)
# ermil    — мужской, размеренный (философия, саморазвитие)
# lea (v3) — эмоциональный женский, лекция (наука, технологии)
NICHE_VOICE_MAP: dict[str, str] = {
    "психология":    "alena",
    "отношения":     "alena",
    "медитация":     "alena",
    "образование":   "alena",
    "бизнес":        "filipp",
    "деньги":        "filipp",
    "финансы":       "filipp",
    "карьера":       "filipp",
    "продуктивность": "filipp",
    "здоровье":      "jane",
    "мотивация":     "jane",
    "спорт":         "jane",
    "питание":       "jane",
    "наука":         "lea",
    "технологии":    "lea",
    "философия":     "ermil",
    "саморазвитие":  "ermil",
    "история":       "ermil",
}


def voice_for_niche(niche: str) -> str | None:
    """Возвращает рекомендуемый Yandex-голос для ниши.
    None — если ниша неизвестна (берётся default из PLATFORM_SETTINGS)."""
    if not niche:
        return None
    return NICHE_VOICE_MAP.get(niche.strip().lower())


def _inject_pauses(text: str) -> str:
    """Заменяет точки и знаки препинания паузами для естественного ритма речи."""
    text = re.sub(r'\.\s+', '... ', text)
    text = re.sub(r'!\s+', '!.. ', text)
    text = re.sub(r'\?\s+', '?.. ', text)
    text = re.sub(r'—\s+', '— ', text)
    return text.strip()


def _text_to_ssml(text: str) -> str:
    """
    Превращает plain-текст в SSML для Yandex SpeechKit.
    Эффекты:
      - <break time="400ms"/> после точек (~естественное дыхание)
      - <break time="500ms"/> после ? (вопрос — пауза для зрителя)
      - <emphasis level="strong"> вокруг чисел/процентов (TTS делает их громче)
      - <break time="200ms"/> вокруг тире (микро-пауза)
    """
    # Escape XML спец-символы
    s = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Emphasis на: числах с % (95%, 23%) ИЛИ числах из 2+ цифр (97, 2023, 140).
    # Один regex — без риска вложенных <emphasis>.
    s = re.sub(
        r"\b\d+\s*%|\b\d{2,}\b",
        lambda m: f'<emphasis level="strong">{m.group()}</emphasis>',
        s,
    )

    # 3. Паузы после конца предложений
    s = re.sub(r"\.(\s|$)", r'.<break time="400ms"/>\1', s)
    s = re.sub(r"\?(\s|$)", r'?<break time="500ms"/>\1', s)
    s = re.sub(r"!(\s|$)", r'!<break time="450ms"/>\1', s)

    # 4. Микро-паузы на тире
    s = re.sub(r"\s+—\s+", r' <break time="200ms"/> ', s)

    # 5. Cleanup: ... превращалось в "..." — для plain-text но в SSML лишнее
    s = s.replace("...", "")

    return f"<speak>{s}</speak>"


def _normalize_volume(path: str) -> None:
    """Нормализует громкость через ffmpeg loudnorm (правит файл на месте)."""
    tmp = path + ".norm.mp3"
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", tmp],
            capture_output=True,
        )
        if result.returncode == 0 and os.path.exists(tmp):
            shutil.move(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------------------------------------------------------------------------
# Kie.ai polling helper (shared by ElevenLabs via Kie.ai)
# ---------------------------------------------------------------------------

_KIE_BASE_URL = "https://api.kie.ai/api/v1"


def _kie_poll(task_id: str, headers: dict, max_wait: int = 300) -> dict | None:
    """Polls queryTaskDetail until succeeded/failed or timeout. Returns inner data dict."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(3)
        try:
            resp = requests.get(
                f"{_KIE_BASE_URL}/jobs/recordInfo",
                headers=headers,
                params={"taskId": task_id},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Kie.ai poll error: %s", exc)
            continue
        inner = data.get("data") or data
        state = str(inner.get("state") or inner.get("status", "")).lower()
        if state in ("succeeded", "success", "completed"):
            return inner
        if state in ("failed", "fail", "error"):
            logger.warning("Kie.ai task failed: %s", inner)
            return None
        elapsed = int(max_wait - (deadline - time.time()))
        if elapsed % 30 < 3:
            logger.info("Kie.ai waiting... state=%s (%ds)", state, elapsed)
    logger.warning("Kie.ai timeout after %ds, task_id=%s", max_wait, task_id)
    return None


def _extract_kie_url(result: dict) -> str | None:
    """Extracts audio URL from Kie.ai succeeded task result.
    Handles resultJson (JSON string) with resultUrls array."""
    import json as _json

    # Parse resultJson — primary format: {"resultUrls": ["https://..."]}
    result_json_str = result.get("resultJson", "")
    if result_json_str:
        try:
            rj = _json.loads(result_json_str)
            urls = rj.get("resultUrls") or rj.get("audioUrls") or rj.get("urls") or []
            if urls and isinstance(urls[0], str):
                return urls[0]
        except Exception:
            pass

    for key in ("result", "output", "url", "audio_url", "audioUrl", "videoUrl"):
        val = result.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
        if isinstance(val, dict):
            for sub in ("url", "audio_url", "audioUrl", "link"):
                u = val.get(sub)
                if isinstance(u, str) and u.startswith("http"):
                    return u
    for v in result.values():
        if isinstance(v, str) and v.startswith("http") and any(
            ext in v for ext in (".mp3", ".wav", ".ogg", ".mp4", ".m4a")
        ):
            return v
    return None


# ---------------------------------------------------------------------------
# Engine 0: ElevenLabs via Kie.ai (приоритет #1)
# ---------------------------------------------------------------------------

def _elevenlabs_kie(text: str, gender: str, output_path: str) -> None:
    """Синтез через ElevenLabs multilingual v2 via Kie.ai async API."""
    if not KIE_API_KEY:
        raise EnvironmentError("KIE_API_KEY не задан в .env")

    voice_name = ELEVENLABS_VOICES[gender]["name"]
    headers = {
        "Authorization": f"Bearer {KIE_API_KEY}",
        "Content-Type": "application/json",
    }

    resp = requests.post(
        f"{_KIE_BASE_URL}/jobs/createTask",
        headers=headers,
        json={
            "model": "elevenlabs/text-to-speech-multilingual-v2",
            "input": {
                "text": text,
                "voice": voice_name,
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.3,
                "speed": 1.0,
                "language_code": "ru",
            },
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    inner = data.get("data") or data
    task_id = inner.get("taskId") or inner.get("task_id") or inner.get("id")
    if not task_id:
        raise RuntimeError(f"Kie.ai ElevenLabs: taskId не найден в ответе: {data}")

    logger.info("Kie.ai ElevenLabs: taskId=%s, ожидаю синтез...", task_id)
    result = _kie_poll(task_id, headers)
    if not result:
        raise RuntimeError("Kie.ai ElevenLabs: задача завершилась с ошибкой или timeout")

    audio_url = _extract_kie_url(result)
    if not audio_url:
        raise RuntimeError(f"Kie.ai ElevenLabs: URL аудио не найден: {result}")

    logger.info("Kie.ai ElevenLabs: скачиваю → %s", output_path)
    dl = requests.get(audio_url, timeout=90, stream=True)
    dl.raise_for_status()
    with open(output_path, "wb") as f:
        for chunk in dl.iter_content(chunk_size=8192):
            f.write(chunk)


# ---------------------------------------------------------------------------
# Engine 0b: ElevenLabs direct API (fallback если нет KIE_API_KEY)
# ---------------------------------------------------------------------------

def _elevenlabs(text: str, gender: str, output_path: str) -> None:
    """Синтез через ElevenLabs REST API (eleven_multilingual_v2, поддерживает русский)."""
    if not ELEVENLABS_API_KEY:
        raise EnvironmentError("ELEVENLABS_API_KEY не задан в .env")

    voice = ELEVENLABS_VOICES[gender]
    resp = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice['id']}",
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        json={
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.4,
                "similarity_boost": 0.75,
                "style": 0.5,
                "use_speaker_boost": True,
            },
        },
        timeout=60,
    )
    resp.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(resp.content)


# ---------------------------------------------------------------------------
# Engine 1: CosyVoice via DashScope (Qwen / Alibaba — бесплатно 500K симв/мес)
# ---------------------------------------------------------------------------

_DASHSCOPE_TTS_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2audio/generation"


def _cosyvoice(text: str, gender: str, output_path: str) -> None:
    """Синтез через CosyVoice-v2 (DashScope). Multilingual, поддерживает русский.
    Бесплатный tier: 500 000 символов/месяц."""
    if not DASHSCOPE_API_KEY:
        raise EnvironmentError("DASHSCOPE_API_KEY не задан в .env")

    voice = COSYVOICE_VOICES[gender]
    # Длинный текст — разбиваем (DashScope лимит ~500 символов на запрос)
    chunks = _split_text_chunks(text, max_chars=450)
    logger.info("CosyVoice: %d чанков, голос=%s", len(chunks), voice)

    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
        "X-DashScope-DataInspection": "disable",
    }

    audio_parts: list[bytes] = []
    for i, chunk in enumerate(chunks):
        resp = requests.post(
            _DASHSCOPE_TTS_URL,
            headers=headers,
            json={
                "model": "cosyvoice-v2",
                "input": {"text": chunk, "voice": voice},
                "parameters": {
                    "format": "mp3",
                    "sample_rate": 22050,
                    "volume": 50,
                    "speed": 1.05,
                    "pitch": 0,
                },
            },
            timeout=60,
        )
        if not resp.ok:
            raise RuntimeError(f"CosyVoice HTTP {resp.status_code}: {resp.text[:300]}")
        # DashScope возвращает audio bytes напрямую
        if len(resp.content) < 100:
            raise RuntimeError(f"CosyVoice: слишком маленький ответ ({len(resp.content)} байт)")
        audio_parts.append(resp.content)

    if len(audio_parts) == 1:
        with open(output_path, "wb") as f:
            f.write(audio_parts[0])
        return

    # Несколько чанков — склеиваем через ffmpeg
    tmp_files: list[str] = []
    try:
        for i, part in enumerate(audio_parts):
            tmp = output_path + f".cosypart{i}.mp3"
            with open(tmp, "wb") as f:
                f.write(part)
            tmp_files.append(tmp)
        list_file = output_path + ".cosyconcat.txt"
        with open(list_file, "w", encoding="utf-8") as f:
            for p in tmp_files:
                f.write(f"file '{p}'\n")
        result = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
             "-codec:a", "copy", output_path],
            capture_output=True,
        )
        os.remove(list_file)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg concat ошибка: {result.stderr.decode(errors='replace')[:200]}")
    finally:
        for p in tmp_files:
            if os.path.exists(p):
                os.remove(p)


# ---------------------------------------------------------------------------
# Engine 2: Silero TTS V5
# ---------------------------------------------------------------------------

def _silero(text: str, gender: str, output_path: str) -> None:
    """Синтез через Silero V5 (локально, CPU). Поддерживает тексты любой длины через чанкинг."""
    try:
        import soundfile  # noqa: F401
        import omegaconf  # noqa: F401
        import scipy  # noqa: F401
    except ImportError:
        import sys
        subprocess.check_call([sys.executable, "-m", "pip", "install", "soundfile", "omegaconf", "silero", "scipy", "torch", "torchaudio", "-q"])

    import torch
    import soundfile as sf

    device = torch.device("cpu")
    model, _ = torch.hub.load(
        repo_or_dir="snakers4/silero-models",
        model="silero_tts",
        language="ru",
        speaker="v5_ru",
        trust_repo=True,
    )
    model.to(device)

    speaker = SILERO_SPEAKER[gender]

    def _generate_chunk(chunk_text: str, wav_path: str) -> None:
        audio = model.apply_tts(text=chunk_text, speaker=speaker, sample_rate=SILERO_SAMPLE_RATE)
        sf.write(wav_path, audio.numpy(), SILERO_SAMPLE_RATE)

    if len(text) <= 900:
        wav_path = output_path.replace(".mp3", ".wav") if output_path.endswith(".mp3") else output_path
        _generate_chunk(text, wav_path)
        if output_path.endswith(".mp3"):
            _wav_to_mp3(wav_path, output_path)
        return

    # Длинный текст — разбиваем на чанки и конкатенируем
    chunks = _split_text_chunks(text, max_chars=900)
    logger.info("Silero: текст %d симв → %d чанков", len(text), len(chunks))
    wav_parts: list[str] = []
    for i, chunk in enumerate(chunks):
        part_wav = output_path.replace(".mp3", f"_chunk{i}.wav")
        _generate_chunk(chunk, part_wav)
        wav_parts.append(part_wav)

    # Конкатенируем WAV через ffmpeg
    list_file = output_path + ".concat.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in wav_parts:
            f.write(f"file '{p}'\n")
    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
         "-codec:a", "libmp3lame", "-q:a", "4", output_path],
        capture_output=True,
    )
    # Чистим временные файлы
    os.remove(list_file)
    for p in wav_parts:
        if os.path.exists(p):
            os.remove(p)
    if result.returncode != 0:
        raise RuntimeError(f"Silero concat ошибка: {result.stderr.decode(errors='replace')}")


def _wav_to_mp3(wav_path: str, mp3_path: str) -> None:
    """Конвертирует WAV → MP3 через ffmpeg."""
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", wav_path, "-codec:a", "libmp3lame", "-q:a", "4", mp3_path],
        capture_output=True,
    )
    os.remove(wav_path)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg WAV→MP3 ошибка: {result.stderr.decode(errors='replace')}")


def _split_text_chunks(text: str, max_chars: int = 900) -> list[str]:
    """Разбивает текст на части ≤max_chars по границам предложений."""
    sentences = re.split(r'(?<=[.!?…])\s+', text.strip())
    chunks: list[str] = []
    current = ""
    for sent in sentences:
        candidate = (current + " " + sent).strip() if current else sent
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # одно предложение длиннее лимита — жёстко обрезаем
            current = sent[:max_chars]
    if current:
        chunks.append(current)
    return chunks or [text[:max_chars]]


# ---------------------------------------------------------------------------
# Engine 2: Yandex SpeechKit (v3 для lea, v1 для alena/filipp/etc.)
# ---------------------------------------------------------------------------

# Голоса доступные только в v3 API (не поддерживаются в v1)
_YANDEX_V3_VOICES = {"lea", "john", "naomi", "masha", "marina"}


def _yandex_v3_chunk(text: str, voice: str, api_key: str, folder_id: str) -> bytes:
    """Синтезирует один чанк текста через SpeechKit v3, возвращает MP3-байты."""
    import json as _json, base64 as _b64
    body = {
        "model": "general",
        "folderId": folder_id,
        "text": text,
        "outputAudioSpec": {"containerAudio": {"containerAudioType": "MP3"}},
        "hints": [{"voice": voice}, {"speed": 1.15}],
        "loudnessNormalizationType": "LUFS",
    }
    resp = requests.post(
        "https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis",
        headers={"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=120,
    )
    if not resp.ok:
        logger.warning("SpeechKit v3 %d: %s", resp.status_code, resp.text[:300])
    resp.raise_for_status()

    audio_bytes = b""
    for line in resp.text.strip().split("\n"):
        if not line.strip():
            continue
        chunk = _json.loads(line)
        b64 = chunk.get("result", {}).get("audioChunk", {}).get("data", "")
        if b64:
            audio_bytes += _b64.b64decode(b64)
    return audio_bytes


def _yandex_v3(text: str, voice: str, output_path: str) -> None:
    """SpeechKit v3 — поддерживает lea и другие современные голоса. Чанкует длинный текст."""
    import tempfile
    api_key = os.getenv("YANDEX_API_KEY", "")
    folder_id = os.getenv("YANDEX_FOLDER_ID", "")
    if not api_key:
        raise EnvironmentError("YANDEX_API_KEY не задан в .env")

    # v3 лимит ~428 байт (240 Кириллических символов); используем 220 для надёжности
    _V3_MAX_CHARS = 220
    chunks = _split_text_chunks(text, max_chars=_V3_MAX_CHARS)
    logger.info("SpeechKit v3: %d чанков для %d симв", len(chunks), len(text))

    if len(chunks) == 1:
        audio = _yandex_v3_chunk(chunks[0], voice, api_key, folder_id)
        if not audio:
            raise RuntimeError("SpeechKit v3 вернул пустое аудио")
        with open(output_path, "wb") as f:
            f.write(audio)
        return

    # Несколько чанков — сохраняем каждый и склеиваем через ffmpeg
    tmp_files: list[str] = []
    try:
        for i, chunk_text in enumerate(chunks):
            audio = _yandex_v3_chunk(chunk_text, voice, api_key, folder_id)
            if not audio:
                raise RuntimeError(f"SpeechKit v3 вернул пустое аудио (чанк {i})")
            tmp = output_path + f".chunk{i}.mp3"
            with open(tmp, "wb") as f:
                f.write(audio)
            tmp_files.append(tmp)

        # Конкатенируем через ffmpeg concat demuxer
        list_file = output_path + ".concat.txt"
        with open(list_file, "w", encoding="utf-8") as f:
            for p in tmp_files:
                f.write(f"file '{p}'\n")
        result = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
             "-codec:a", "copy", output_path],
            capture_output=True,
        )
        os.remove(list_file)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg concat ошибка: {result.stderr.decode(errors='replace')[:200]}")
    finally:
        for p in tmp_files:
            if os.path.exists(p):
                os.remove(p)


def _track_voice_cost(text: str, engine: str) -> None:
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from utils.cost_tracker import log_cost
        chars = len(text)
        if engine == "yandex":
            log_cost("voice_agent", "yandex_speechkit", "chars", chars)
        elif engine == "elevenlabs":
            log_cost("voice_agent", "elevenlabs", "chars", chars)
        # silero / edge-tts are free
    except Exception:
        pass


def _yandex_v1(text: str, voice: str, output_path: str, use_ssml: bool = True) -> None:
    """
    SpeechKit v1 — alena, filipp, jane и другие классические голоса.
    use_ssml=True — отправляем text с SSML-разметкой (паузы, emphasis на цифрах).
                    Это даёт ~30% более естественную интонацию.
    """
    api_key = os.getenv("YANDEX_API_KEY", "")
    if not api_key:
        raise EnvironmentError("YANDEX_API_KEY не задан в .env")

    base_data = {
        "lang": "ru-RU",
        "voice": voice,
        "emotion": "neutral",
        "speed": "1.15",
        "format": "mp3",
        "sampleRateHertz": "48000",
    }
    if use_ssml:
        base_data["ssml"] = _text_to_ssml(text)
    else:
        base_data["text"] = text

    resp = requests.post(
        "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize",
        headers={"Authorization": f"Api-Key {api_key}"},
        data=base_data,
        timeout=60,
    )
    if resp.status_code != 200 and use_ssml:
        # Если SSML не принят (parse error etc.) — fallback на plain text
        logger.warning("Yandex v1 SSML отвергнут (%d): %s — fallback на plain text",
                       resp.status_code, resp.text[:200])
        return _yandex_v1(text, voice, output_path, use_ssml=False)
    resp.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(resp.content)


def _yandex(text: str, voice: str, output_path: str) -> None:
    """Маршрутизирует на v3 (lea) или v1 (alena и др.) в зависимости от голоса."""
    if voice in _YANDEX_V3_VOICES:
        _yandex_v3(text, voice, output_path)
    else:
        _yandex_v1(text, voice, output_path)


# ---------------------------------------------------------------------------
# Engine 3: edge-tts (резерв)
# ---------------------------------------------------------------------------

async def _edge_tts_async(text: str, voice: str, rate: str, output_path: str) -> None:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate, volume="+0%")
    await communicate.save(output_path)


def _edge_tts(text: str, voice: str, rate: str, output_path: str) -> None:
    import time
    last_exc: Exception = RuntimeError("edge-tts: нет попыток")
    for attempt in range(3):
        try:
            asyncio.run(_edge_tts_async(text, voice, rate, output_path))
            return
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                logger.warning("edge-tts попытка %d/3 ошибка: %s — повтор через 2 сек", attempt + 1, exc)
                time.sleep(2)
    raise last_exc


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def generate_voice(
    full_script: str,
    platform: Platform,
    voice: str | None = None,
) -> dict:
    """
    Генерирует MP3 из текста сценария.

    Args:
        full_script: Полный текст для синтеза
        platform:    Целевая платформа
        voice:       Переопределить голос (edge-tts voice name или silero speaker)

    Returns:
        dict: audio_path, voice, platform, engine (silero|yandex|edge-tts)

    Raises:
        ValueError: если platform не поддерживается или текст пустой
        RuntimeError: если все три движка недоступны
    """
    if platform not in PLATFORM_SETTINGS:
        raise ValueError(
            f"Неподдерживаемая платформа: {platform}. "
            f"Допустимые: {list(PLATFORM_SETTINGS.keys())}"
        )
    if not full_script or not full_script.strip():
        raise ValueError("full_script не может быть пустым")

    settings = PLATFORM_SETTINGS[platform]
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = os.path.abspath(os.path.join(OUTPUT_DIR, f"{timestamp}_{platform}.mp3"))

    gender = settings["gender"]
    script_with_pauses = _inject_pauses(full_script)

    from utils.providers import is_enabled, mark_dead, looks_like_no_credits

    # ── Engine 0: ElevenLabs via Kie.ai (приоритет #1, платный) ─────────────
    if KIE_API_KEY and is_enabled("kie"):
        try:
            el_voice = ELEVENLABS_VOICES[gender]
            logger.info("Voice Agent: ElevenLabs via Kie.ai (voice=%s)", el_voice["name"])
            _elevenlabs_kie(script_with_pauses, gender, output_path)
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                raise RuntimeError("Kie.ai ElevenLabs не создал файл")
            _normalize_volume(output_path)
            _track_voice_cost(full_script, "elevenlabs")
            logger.info("Voice Agent: ElevenLabs via Kie.ai ✓ → %s", output_path)
            return {
                "audio_path": output_path,
                "voice": el_voice["name"],
                "platform": platform,
                "engine": "elevenlabs-kie",
            }
        except Exception as exc:
            if looks_like_no_credits(exc):
                mark_dead("kie", str(exc)[:200])
            logger.warning("Voice Agent: ElevenLabs via Kie.ai ошибка (%s) — пробую Yandex", exc)

    # ── Engine 1: CosyVoice via DashScope (Qwen, бесплатно) ─────────────────
    if DASHSCOPE_API_KEY and is_enabled("dashscope"):
        try:
            cv_voice = COSYVOICE_VOICES[gender]
            logger.info("Voice Agent: CosyVoice v2 via DashScope (voice=%s)", cv_voice)
            _cosyvoice(script_with_pauses, gender, output_path)
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                raise RuntimeError("CosyVoice не создал файл")
            _normalize_volume(output_path)
            logger.info("Voice Agent: CosyVoice ✓ → %s", output_path)
            return {
                "audio_path": output_path,
                "voice": cv_voice,
                "platform": platform,
                "engine": "cosyvoice",
            }
        except Exception as exc:
            logger.warning("Voice Agent: CosyVoice ошибка (%s) — пробую Yandex", exc)
    else:
        logger.info("Voice Agent: DASHSCOPE_API_KEY не задан — пропускаю CosyVoice")

    # ── Engine 2: Yandex SpeechKit (платный) ─────────────────────────────────
    if os.getenv("YANDEX_API_KEY", "") and is_enabled("yandex_tts"):
        try:
            yandex_voice = voice if voice and voice not in SILERO_SPEAKER.values() else settings["yandex_voice"]
            logger.info("Voice Agent: Yandex SpeechKit (voice=%s, SSML emphasis)", yandex_voice)
            # SSML внутри сам делает паузы — передаём оригинальный текст, не _inject_pauses
            _yandex(full_script, yandex_voice, output_path)
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                raise RuntimeError("Yandex не создал файл")
            _normalize_volume(output_path)
            _track_voice_cost(full_script, "yandex")
            logger.info("Voice Agent: Yandex ✓ → %s", output_path)
            return {"audio_path": output_path, "voice": yandex_voice, "platform": platform, "engine": "yandex"}
        except Exception as exc:
            if looks_like_no_credits(exc):
                mark_dead("yandex_tts", str(exc)[:200])
            logger.warning("Voice Agent: Yandex ошибка (%s) — пробую edge-tts", exc)
    else:
        logger.info("Voice Agent: Yandex SpeechKit выключен (budget mode или нет ключа) — пропускаю")

    # ── Engine 3: edge-tts (бесплатный, качество выше Silero) ────────────────
    try:
        if voice and voice.startswith("ru-RU"):
            edge_voice = voice
        elif voice in EDGE_PERSONA_MAP:
            # Персона ниши (alena/filipp/...) транслируется в edge-голос
            edge_voice = EDGE_PERSONA_MAP[voice]
        else:
            edge_voice = settings["edge_voice"]
        logger.info("Voice Agent: edge-tts (voice=%s, rate=%s)", edge_voice, settings["rate"])
        _edge_tts(script_with_pauses, edge_voice, settings["rate"], output_path)
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError("edge-tts не создал файл")
        _normalize_volume(output_path)
        logger.info("Voice Agent: edge-tts ✓ → %s", output_path)
        return {"audio_path": output_path, "voice": edge_voice, "platform": platform, "engine": "edge-tts"}
    except Exception as exc:
        logger.warning("Voice Agent: edge-tts ошибка (%s) — пробую Silero (локальный резерв)", exc)

    # ── Engine 4: Silero (локальный офлайн-резерв) ───────────────────────────
    silero_speaker = voice if voice in SILERO_SPEAKER.values() else SILERO_SPEAKER[gender]
    logger.info("Voice Agent: Silero TTS V5 (speaker=%s)", silero_speaker)
    _silero(script_with_pauses, gender, output_path)
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError("Silero не создал файл")
    _normalize_volume(output_path)
    logger.info("Voice Agent: Silero ✓ → %s", output_path)
    return {"audio_path": output_path, "voice": silero_speaker, "platform": platform, "engine": "silero"}


if __name__ == "__main__":
    import sys
    import json

    logging.basicConfig(level=logging.INFO)

    script = sys.argv[1] if len(sys.argv) > 1 else (
        "Ты знаешь, почему 5% людей достигают 95% успеха? "
        "Продуктивные люди планируют день заранее, расставляют приоритеты, "
        "автоматизируют рутину, делают перерывы и постоянно учатся. "
        "Напиши в комментарии — какую привычку хочешь внедрить первой?"
    )
    platform = sys.argv[2] if len(sys.argv) > 2 else "tiktok"

    print(f"Синтезирую голос для {platform}...\n")
    result = generate_voice(script, platform)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nФайл создан: {result['audio_path']}")

"""
Маскирование секретов (utils/log_redact): все ключи из .env.example,
форматы ключей провайдеров, ключи в URL, заголовках и dict/JSON.
Значения ниже — синтетические, не настоящие ключи.
"""

import logging
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from utils import log_redact
from utils.log_redact import RedactingFilter, redact

ROOT = Path(__file__).resolve().parent.parent

_FAKE = "Zx9" + "q" * 20 + "W7v"   # 26 символов, без «узнаваемого» префикса


def _secret_vars_from_example() -> list[str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    names = re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M)
    return [n for n in names if n.endswith(log_redact._SECRET_ENV_SUFFIXES)]


def test_env_example_has_expected_secrets():
    names = set(_secret_vars_from_example())
    for must in ("KIE_API_KEY", "GEMINI_API_KEY", "ZERNIO_API_KEY", "TELEGRAM_BOT_TOKEN",
                 "VK_USER_TOKEN", "ELEVENLABS_API_KEY", "YANDEX_API_KEY", "PEXELS_API_KEY",
                 "YOUTUBE_API_KEY", "UNSPLASH_ACCESS_KEY", "ADMIN_TOKEN"):
        assert must in names


@pytest.mark.parametrize("name", _secret_vars_from_example())
def test_every_env_example_secret_is_masked(name):
    value = f"{name.lower()}-{_FAKE}"
    with patch.dict("os.environ", {name: value}):
        out = redact(f"request failed: {value} (and again {value})")
    assert value not in out
    assert out.count("***") == 2


def test_dotenv_file_values_masked_without_load_dotenv(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"KIE_API_KEY=kie-{_FAKE}\nTELEGRAM_CHAT_ID=12345678\n", encoding="utf-8")
    log_redact._dotenv_secrets.cache_clear()
    try:
        with patch.object(log_redact, "_ENV_FILE", env):
            out = redact(f"boom kie-{_FAKE} chat 12345678")
    finally:
        log_redact._dotenv_secrets.cache_clear()
    assert _FAKE not in out
    assert "12345678" in out          # chat id — не секрет, не трогаем


@pytest.mark.parametrize("secret", [
    "sk-or-v1-" + "a1b2c3d4" * 8,                 # OpenRouter
    "sk-" + "0123456789abcdef" * 2,                # DashScope / OpenAI-подобные
    "sk_" + "0123456789abcdef" * 3,                # ElevenLabs
    "gsk_" + "AbCdEf0123456789" * 3,               # Groq
    "nvapi-" + "AbCd-Ef01_2345" * 4,               # NVIDIA
    "csk-" + "abcdef0123456789" * 3,               # Cerebras
    "tvly-" + "AbCdEf0123456789" * 2,              # Tavily
    "vk1.a." + "AbCdEf0123456789_-" * 5,           # VK
    "AQVN" + "AbCdEf0123456789" * 2,               # Yandex Cloud API key
    "t1." + "AbCdEf0123456789." * 4,               # Yandex IAM
    "AIzaSy" + "A1b2C3d4E5" * 3 + "xyz",           # Google / Gemini / YouTube
    "1234567890:AA" + "Hb" * 17,                   # Telegram bot token
])
def test_provider_key_formats_masked(secret):
    out = redact(f"error with {secret} here")
    assert secret not in out
    assert "here" in out


@pytest.mark.parametrize("url,leak", [
    ("https://api.telegram.org/bot1234567890:AAH" + "x" * 32 + "/sendMessage", "AAH" + "x" * 32),
    ("https://www.googleapis.com/youtube/v3/videos?part=snippet&key=SomeKey1234567890", "SomeKey1234567890"),
    ("https://api.unsplash.com/search/photos?query=sea&client_id=UnspLashKey1234567", "UnspLashKey1234567"),
    ("https://freemusicarchive.org/api/get/tracks.json?api_key=FmaKey12345678&limit=5", "FmaKey12345678"),
    ("https://api.vk.com/method/wall.post?access_token=VkToken1234567890&v=5.199", "VkToken1234567890"),
    ("https://cdn.example.com/f.mp4?X-Amz-Signature=deadbeef1234&sig=abcdef987654", "abcdef987654"),
])
def test_keys_in_urls_masked(url, leak):
    out = redact(f"404 Client Error: Not Found for url: {url}")
    assert leak not in out
    assert "for url: https://" in out


@pytest.mark.parametrize("text,leak", [
    ("{'Authorization': 'Bearer kieTOKEN1234567890'}", "kieTOKEN1234567890"),
    ("{'Authorization': 'Api-Key yandexKEY12345678'}", "yandexKEY12345678"),
    ("{'Authorization': 'pexelsRawKey123456789'}", "pexelsRawKey123456789"),
    ('{"xi-api-key": "elevenKEY123456789"}', "elevenKEY123456789"),
    ("{'X-Goog-Api-Key': 'gemKEY1234567890'}", "gemKEY1234567890"),
    ("{'X-Subscription-Token': 'braveTOKEN12345'}", "braveTOKEN12345"),
    ('{"X-API-KEY": "serperKEY1234567"}', "serperKEY1234567"),
    ('{"api_key": "tavilyKEY123456789", "query": "x"}', "tavilyKEY123456789"),
    ("{'access_token': 'vkTOKEN123456789', 'v': '5.199'}", "vkTOKEN123456789"),
    ("Authorization: OAuth y0_" + "A" * 40, "y0_" + "A" * 40),
    ("xi-api-key: elevenRawHeader123", "elevenRawHeader123"),
])
def test_keys_in_headers_and_json_masked(text, leak):
    out = redact(text)
    assert leak not in out


@pytest.mark.parametrize("text", [
    "{'key': 'subtitles', 'label': 'Субтитры', 'status': 'done'}",
    "Шаг 'voice' готов за 12.5 с, 2026-10-23 03:14:15",
    "ffmpeg -i output/video/kabinet7_20261021_233042.mp4 -vf ass=subs.ass",
    "token count: 1234, model gemini-3.8-flash",
])
def test_ordinary_text_untouched(text):
    assert redact(text) == text


def test_redacting_filter_masks_args_traceback_and_stack():
    url = "https://api.telegram.org/bot123456789:AA" + "x" * 33 + "/send"
    try:
        raise RuntimeError(f"failed {url}")
    except RuntimeError:
        import sys
        exc_info = sys.exc_info()
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "fail: %s", (url,), exc_info)
    record.stack_info = f"Stack: {url}"
    RedactingFilter().filter(record)
    assert "x" * 33 not in record.getMessage()
    assert "x" * 33 not in record.exc_text
    assert "x" * 33 not in record.stack_info

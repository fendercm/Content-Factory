# Безопасность — аудит 2026-10-23

Продолжение аудита 2026-10-18. Без платных и сетевых вызовов к сервисам проекта;
история git не переписывалась.

## 1. Секреты

**Найдено**
- В коде, тестах и `.env.example` настоящих ключей нет (в `.env.example` — заглушки `your_…`,
  в тестах — синтетические значения).
- **Утечка в истории публичного репозитория** github.com/fendercm/Content-Factory, коммит
  `c6bf120` (Initial commit), удалены из дерева в `7750474`, но остаются в истории:
  - `logs/pipeline_20260511.log` — `TELEGRAM_BOT_TOKEN` (4 вхождения, URL `api.telegram.org/bot…`);
  - `logs/pipeline_20260523.log` — `YOUTUBE_API_KEY` (9 вхождений, `?key=AIza…`).
  - `pipeline.db` и `.claude/` из того же коммита секретов не содержат.
- Локально те же два лога лежат в `output/_archive_2026-10-19/logs/` (не в git). Текущие `logs/`
  и `pipeline.db` — чистые.

**Исправлено** — `utils/log_redact.py`:
- значения секретов берутся не только из `os.environ`, но и напрямую из `.env` (только чтение) —
  маскирование работает, даже если `load_dotenv` ещё не вызван;
- форматы ключей: OpenRouter/DashScope `sk-…`, ElevenLabs `sk_…`, Groq `gsk_…`, NVIDIA `nvapi-…`,
  Cerebras `csk-…`, Tavily `tvly-…`, VK `vk1.a.…`, Yandex `AQVN…`/`t1.…`/`y0_…`, Google `AIza…`, Telegram;
- ключи в URL: добавлены `client_id` (Unsplash), `access_token`, `api_key`, `secret`, `sig` и др.;
- заголовки и dict/JSON: `Authorization` (в т.ч. «сырой» ключ Pexels), `xi-api-key`, `X-Goog-Api-Key`,
  `X-Subscription-Token`, `X-API-KEY`, `api_key`, `access_token`, `OAuth …`;
- фильтр логов маскирует и `stack_info`.
- Тесты: `tests/test_log_redact.py` — каждый секрет из `.env.example`, все форматы, URL, заголовки,
  отсутствие ложных срабатываний на обычном тексте.

**Осталось — за пользователем**
- Перевыпустить `TELEGRAM_BOT_TOKEN` (@BotFather → /revoke) и `YOUTUBE_API_KEY`
  (Google Cloud Console → Credentials → Regenerate), обновить `.env`.
- Сделать репозиторий приватным или переписать историю (`git filter-repo --path logs --path pipeline.db
  --invert-paths` + force-push) — только после ротации, ротация обязательна в любом случае.
- Удалить/почистить `output/_archive_2026-10-19/logs/pipeline_202605{11,23}.log`.

## 2. Зависимости

pip-audit (в изолированном venv) по текущему окружению нашёл уязвимости в: pillow 12.2.0 (48),
urllib3 1.26.20 (7), aiohttp 3.13.5 (22), werkzeug 3.1.8 (`safe_join` и имена устройств Windows),
idna, certifi, torch 2.11.0, а также в пакетах вне проекта (pypdf, starlette, cryptography и др.).

**Исправлено** — `requirements.txt`: все версии закреплены `==` по `pip freeze`; уязвимые подняты
до исправленных (pillow 13.0.0, urllib3 2.8.0, werkzeug 3.1.9, aiohttp 3.14.5, idna 3.20,
certifi ≥ 2026.10.19). Убраны неиспользуемые `groq` и `google-genai` (Gemini вызывается через REST,
Groq отключён). Полный набор тестов прогнан на обновлённых пакетах (overlay-venv) — зелёный;
`pip-audit -r requirements.txt` — «No known vulnerabilities found».

**Осталось — за пользователем**: окружение ещё на старых версиях — выполнить
`py -m pip install -r requirements.txt` и прогнать тесты. torch (зависимость whisper) < 2.13 уязвим
в `torch.load`; риск низкий (whisper грузит свою модель с проверкой sha256), обновить при случае.
`agents/voice_agent.py` при отсутствии Silero ставит пакеты `pip install` без версий прямо во время
работы — лучше убрать автоустановку.

## 3. Внешние вызовы

- Все 67 вызовов `requests.*` — с `timeout`. `verify=False` / отключения TLS нигде нет.
  Клиент `OpenAI(...)` в `utils/llm_client.py` создаётся без явного `timeout` (по умолчанию 600 с) —
  файл правит другая сессия, не трогал.
- **Исправлено** `utils/download.py`: только `http(s)`, лимит размера `max_bytes` (2 ГБ по умолчанию,
  проверка `Content-Length` и по факту) — ответ внешнего API не забьёт диск.
- **Исправлено** `utils/kie_client.upload_file`: наружу уходят только медиафайлы
  (jpg/png/webp/mp3/wav/m4a/ogg/aac/mp4/mov/webm) и только существующие. Файл получает публичную
  ссылку на 24 часа на `kieai.redpandaai.co` (запасной — `api.kie.ai`) вместе с Bearer-ключом KIE.
  Имена файлов заказов уже санитизируются в `order_agent.add_file` и сверяются со списком `order["files"]`.
- **Осталось** (agents/* правят другие сессии): скачивания мимо `utils/download.py` —
  `agents/music_agent.py:270` (Suno), `agents/voice_agent.py:303` (ElevenLabs через KIE),
  `agents/image_agent.py:77` (целиком в память), `scripts/*_bakeoff.py`, `scripts/download_music.py`.

## 4. Подпроцессы и FFmpeg

- Все вызовы `subprocess` — списком аргументов, `shell=True`/`os.system` нет.
- **Осталось** (не исправлено — agents/* правят другие сессии):
  - `agents/assembly_agent.py:_esc` экранирует только `:`; путь с `'` ломает
    `subtitles='…'` (`assembly_agent.py:1256,1360`, `series_assembly.py:179`). Правильно:
    `p.replace("\\","/").replace(":","\:").replace("'","\'").replace("'","'\''")`
    (экранирование уровня опции, затем уровня графа) + тест с путём `O'Brien`.
  - concat-списки `file '{p}'` (`voice_agent.py:409,477,599`, `series_assembly.py:156`) не экранируют
    `'` — нужно `p.replace("'", "'\''")`; `-safe 0` разрешает любые пути.
  - `drawtext ... fontfile='{font_path}'` (`assembly_agent.py:1284`) — путь без экранирования.

## 5. Админ-панель (`utils/api_server.py`, `admin_panel.html`) — не менялись

Файлы сейчас правят другие сессии (panel-lab, money-guard); детальную проверку CSRF/Host/токена,
path traversal в `/api/video` и `/api/scripts/<name>` и XSS в панели нужно провести отдельно после
их слияния. Пункты для проверки: сравнение токена через `hmac.compare_digest`; разрешение пути через
`Path.resolve()` + проверка, что он внутри разрешённой папки (а не только `..` в строке), с учётом
имён устройств Windows (CON, NUL — исправлено в werkzeug 3.1.9); вывод данных из БД/LLM в HTML только
через `textContent`/экранирование; `redact()` уже применяется к ошибкам и строкам логов в ответах API.

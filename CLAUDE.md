# Content Factory — Проектный контекст

## Идея проекта
Автоматический контент-завод: ИИ генерирует видео и публикует их на несколько аккаунтов в нескольких социальных сетях без ручного труда.

**Цель:** полностью автономный пайплайн от идеи до публикации, работающий по расписанию.

---

## Архитектура — 7 слоёв, 16 агентов

### Слой 1 — Мозг (Orchestration)
- **Master Orchestrator** (`main.py`) — главный дирижёр. CLI: `py main.py --niche "..."` или `--niches "психология,бизнес"` (параллельный запуск через threading). Retry 3x → Telegram алерт. Сохраняет статус в `pipeline.db`.
- **Scheduler** (`scheduler.py`) — запускает пайплайн 3 раза в день (09:00, 14:00, 20:00). Ежедневная сводка в Telegram в 21:00. `py scheduler.py` (работает бесконечно). Зависимость: `pip install schedule`.
- **Analytics Agent** (`agents/analytics_agent.py`) — раз в сутки анализирует перформанс, передаёт сигналы Orchestrator'у.

### Слой 2 — Идеи (Research)
- **Trend Scout** (`agents/trend_scout.py`) — парсит Google Trends (pytrends) и Reddit (PRAW). Без LLM, бесплатно. API: `run(niche: str) → list[{topic, virality, source}]` топ-5.
- **Idea Generator** (`agents/idea_generator.py`) — принимает темы от Trend Scout, генерирует топ-3 идеи через Groq. API: `run(topics) → list[{title, hook, angle, hashtags, source_topic, virality}]`.
- **Topic Filter** (`agents/topic_filter.py`) — проверяет идеи: дедупликация по SQLite (30 дней) + safety через Gemini 2.0 Flash → Cerebras fallback. Жёстко режет стоп-категории (политика, мед-советы, 18+). API: `run(ideas, platform, db_path, niche) → list[idea]`.
- **Research Agent** (`agents/research_agent.py`) — собирает факты по теме из 4 источников с confidence-метками:
  1. Wikipedia RU + EN (`high`)
  2. Semantic Scholar (`high`)
  3. Google News RSS (`low`)
  4. Web Search (`medium`) с автопереключением: Tavily → Brave → Serper → DuckDuckGo (через `ddgs`).
  API: `research(topic) → {facts: list[{text, source, confidence, url}], summary, sources_used}`.

### Слой 3 — Сценарий (Script)
- **Script Writer** (`agents/script_writer.py`) — генерирует сценарий 100-115 слов (45 сек видео) через Yandex DeepSeek-V3 → Groq fallback. Структура: HOOK (4 формулы) → SETUP (1-е лицо) → КОНТРАСТ 1 → КОНТРАСТ 2 → CTA-вопрос. Стиль: дерзкий-вызывающий. Использует facts с confidence-метками, может цитировать "Стэнфорд 2023" + классиков из whitelist (Юнг/Фрейд/Дарвин/Эйнштейн/Кант/Ницше/Сократ/Платон/Аристотель/Тесла/Павлов/Скиннер/Ферми/Кюри). Стоп-категории: политика, мед-советы, 18+. API: `write_script(topic, platform, research_context?) → {hook, body, cta, full_script}`.
- **Fact Checker** (`agents/fact_checker.py`) — мягкий редактор-фактчекер. Пропускает: hedge-маркеры, классиков, "учёные из Стэнфорда/Гарварда". Флагует только реальные имена с явно неверной профессией. При невалидном JSON делает 4-уровневый fallback (markdown extract → keyword detect → pass).

### Слой 4 — Медиа (Production)
- **Voice Agent** (`agents/voice_agent.py`) — TTS: ElevenLabs → Yandex SpeechKit (lea/alena) → Silero → edge-tts.
- **Music Agent** (`agents/music_agent.py`) — подбирает фоновую музыку по настроению (upbeat/calm/dramatic). Локальная папка + FMA API. В `assembly_agent` микшируется sidechain-компрессией.
- **Visual Agent** (`agents/visual_agent.py`) — два метода:
  - `get_visuals(topic, platform, num_clips=14, script_text)` — поблочные Pexels-видео-запросы по сценарию, жёсткий фильтр portrait-ориентации.
  - `get_photos(topic, platform, n=17, script_text)` — фото из Pexels Photos + Unsplash (если `UNSPLASH_ACCESS_KEY`), с perceptual-hash дедупом и quality-фильтром (min 800x1100 portrait).
- **Entity Visuals Agent** (`agents/entity_visuals.py`) — сканирует сценарий на упоминания **14 классиков** + **12 институтов** (Stanford, Harvard, MIT, Yale, WHO, NASA, Oxford, Cambridge, Princeton, Columbia, ВОЗ, MIT). Тянет фото из Wikipedia Pageimages API (ru → en fallback, thumbnail 1024px). Использует Whisper word-timestamps для точных оверлеев (±0.1с). API: `run(script_text, audio_duration, word_timestamps?) → {overlays: [{name, photo_path, start_sec, duration}], found}`.
- **Subtitle Agent** (`agents/subtitle_agent.py`) — Whisper-base транскрипция с word_timestamps. ASS-субтитры: Impact 96pt, pop-bounce анимация, каждое 3-е слово жёлтое. API возвращает `{subtitle_path, segment_count, words}`.
- **Assembly Agent** (`agents/assembly_agent.py`) — собирает MP4 через FFmpeg. **Hybrid Timeline (22 слота для 45с видео)**:
  - Slot 0 = hook (защищён)
  - Entity overlays (классики/институты) — фиксированные слоты по тайм-кодам
  - Photo burst (5 фото × 0.4с hard-cut) — посередине
  - 50/50 weave: чётные слоты = фото, нечётные = клипы
  - Pacing 2.0 сек/сегмент, mix-переходы (hard cut 60% / whip 20% / circle 5% / flash 5% / slide 5% / dissolve 5%)
  - Color grade: `eq=saturation=1.18:contrast=1.06`, мягкая виньетка PI/6
  - Encode: H.264 High@4.1, CRF 19, preset medium, AAC 192k @ 48kHz
  - Audio: loudnorm -16 LUFS + sidechain-компрессия музыки под голос
  - Photo pre-grade: `eq=saturation=1.12:contrast=1.08` + `curves=increase_contrast` — фото визуально матчится с клипами
  API: `assemble_video(audio_path, platform, clips, subtitle_path?, music_path?, entity_overlays?, photo_burst?, photo_pool?)`.
- **Thumbnail Agent** (`agents/thumbnail_agent.py`) — YandexART → Pixazo → Pillow.

### Слой 5 — Контроль качества (QA)
- **QA Agent** (`agents/qa_agent.py`) — проверяет MP4 через ffprobe без LLM: длительность, соотношение сторон, наличие видео-стрима, непустой файл. API: `check(video_path, platform, expected_duration?) → {"status": "approved"} | {"status": "rejected", "reason": "..."}`.
- **Safety Agent** (`agents/safety_agent.py`) — проверяет сценарий и метаданные через Gemini 2.0 Flash на hate speech, насилие, NSFW, copyright, дезинформацию. Учитывает правила конкретной платформы. API: `check(script_text, title, hashtags, platform) → {"safe": True} | {"safe": False, "issue": "...", "category": "..."}`.
- **Analytics Agent** (`agents/analytics_agent.py`) — запускается **отдельно** раз в сутки (`py agents/analytics_agent.py --days 7`). Забирает статистику через Zernio API, делает upsert в таблицу `performance`, возвращает топ-3 темы по просмотрам. API: `run(days, db_path?) → list[{post_id, title, platform, views, likes, comments}]`.

### Слой 6 — Публикация (Distribution)
- **Publisher Agent** (`agents/publisher_agent.py`) — публикует через Zernio API. API: `publish_video(video_path, title, description, hashtags, platform, account_id) → {post_id, status, platform}`.
- **Notify Agent** (`agents/notify_agent.py`) — Telegram бот с HTML-escape. API: `send(message, chat_id?, level?)`, `send_photo(bytes, caption)`, `alert_error(step, error, attempt)`, `alert_success(title, platforms, post_id, duration?, video_path?)`. При успехе отправляет карточку с превью кадром из видео.

### Слой 7 — Память (Memory)
- **Memory & State Store** — SQLite `pipeline.db`. Таблицы: `topics_used`, `pipeline_state`, `performance`, `error_log`. Схема инициализируется автоматически при первом запуске `main.py`.

---

## Запуск пайплайна

```bash
# Полный пайплайн с публикацией
py main.py --niche "психология"

# Другая платформа
py main.py --niche "бизнес" --platform youtube

# Без реальной публикации (тест)
py main.py --niche "психология" --dry-run

# Несколько ниш параллельно (threading)
py main.py --niches "психология,бизнес,здоровье" --platform tiktok

# Story-формат: серийная история с лором (мир живёт в pipeline.db)
py main.py --niche "тёмное фэнтези" --format story --dry-run

# Планировщик (3 раза в день + ежедневная сводка в 21:00)
py scheduler.py

# Доступные платформы: tiktok | instagram | youtube | linkedin | twitter
```

Порядок шагов в пайплайне (актуальный):
1. `trend_scout` — топ-5 трендовых тем
2. `idea_generator` — топ-3 идеи
3. `topic_filter` — дедуп 30д + niche-relevance (Cerebras) + safety (Gemini → Cerebras)
4. `research_agent` — собирает факты (Wikipedia + Web Search + Scholar + News) с confidence
5. `script_writer` — 100-115 слов, дерзкий тон, 1-е лицо, hook-микс из 4 формул
6. `fact_checker` — мягкий чек с 4-уровневым JSON fallback. Если провал — следующая идея
7. `safety_agent` — стоп-категории (политика/мед/18+)
8. `voice_agent` — ElevenLabs → Yandex SpeechKit → edge-tts
9. `music_agent` — фон по настроению (опционально)
10. `visual_agent.get_visuals` — 14 видео-клипов Pexels по сценарию (portrait filter)
11. `subtitle_agent` — Whisper ASS субтитры + word_timestamps
12. `entity_visuals` — фото классиков+институтов из Wikipedia (точные тайминги по Whisper)
13. `visual_agent.get_photos` — 17 фото Pexels + Unsplash (perceptual-hash dedup)
14. `assembly_agent` — hybrid timeline (50/50 weave clip↔photo + entity overlays + photo burst)
15. `thumbnail_agent` — YandexART/Pixazo/Pillow (опционально)
16. `qa_agent` — ffprobe
17. `publisher_agent` — Zernio (publishNow=True)
18. `notify_agent` — Telegram карточка

```bash
# Аналитика (запускать отдельно, раз в сутки)
py agents/analytics_agent.py --days 7
```

---

## Admin Panel v2 — API Endpoints

Запуск: `start.bat` или `py utils/api_server.py` (порт 5000).

| Метод | Endpoint | Описание |
|---|---|---|
| GET | /api/stats | KPI: видео, ошибки, аккаунты |
| GET | /api/runs?limit=50 | История запусков |
| GET | /api/agents | Статистика агентов |
| GET | /api/pipeline | Статус шагов последнего запуска |
| GET | /api/logs?lines=100 | Последние строки лог-файлов |
| GET | /api/messages?since=ISO&limit=200 | Сообщения агентов из agent_messages |
| GET | /api/stream | SSE: живые сообщения + pipeline каждую секунду |
| POST | /api/run | Запустить пайплайн `{niche, platform, dry_run, custom_topic}` |
| POST | /api/run/stop | Остановить текущий subprocess |
| GET | /api/run/status | Статус текущего процесса + pid |
| GET | /api/ideas?status=&limit=200 | Все идеи из pipeline_state с раскрытием сценария |
| POST | /api/test/\<agent\> | Быстрый тест агента с mock-данными |

**Новые таблицы в pipeline.db:**
- `agent_messages` — (id, timestamp, sender, receiver, message, run_id)

**Новые файлы:**
- `utils/agent_logger.py` — `log_message()` + `set_run_id()` для записи в agent_messages
- `tests/test_agents.py` — быстрые тесты всех агентов с mock-данными, вызываются из /api/test/\<agent\>

**Страницы Admin Panel:**
- **Dashboard** — KPI + Launch Panel (запуск из UI + progress bar) + мини-лента + последние запуски
- **Feed** — полная живая лента сообщений агентов (SSE, цвета по агенту)
- **Pipeline** — анимированная визуализация 3×5 узлов, пульс при работе, стрелки с анимацией
- **Ideas** — таблица всех идей, раскрытие строки показывает полный сценарий
- **Agents** — статистика запусков/ошибок каждого агента
- **Tests** — запуск тестов по агентам кнопкой прямо из UI

---

## Технический стек

| Компонент | Инструмент | Стоимость |
|---|---|---|
| LLM — сценарии | Yandex AI Studio DeepSeek-V3 (основной) | платно |
| LLM — фильтрация/идеи | Yandex AI Studio YandexGPT-Lite (основной) | платно |
| LLM — fallback | Groq (Llama 3.3 70B) | бесплатно |
| LLM — fallback 2 | Cerebras (1M токенов/день) | бесплатно |
| LLM — safety/filter | Gemini 2.0 Flash (google-genai SDK) | бесплатно |
| Голос | Yandex SpeechKit (lea/alena) → Silero → edge-tts | платно |
| Превью (thumbnail) | YandexART ($0.018/шт) → Pixazo → Pillow | платно |
| Stock footage | Pexels API | бесплатно |
| Видео-монтаж | FFmpeg | бесплатно |
| Тренды | pytrends (Google Trends) | бесплатно |
| Reddit | PRAW | бесплатно |
| БД/состояние | SQLite | бесплатно |
| Уведомления | Telegram Bot API | бесплатно |
| Публикация | Zernio API | платно |

LLM цепочка: Yandex AI Studio → Groq → Cerebras (всё настраивается через `utils/llm_client.py`)
- `script_writer`, `fact_checker` — `deepseek-v3` (лучшее качество)
- остальные агенты — `yandexgpt-lite` (дешевле)

---

## Переменные окружения (.env)

```
# Yandex AI Studio (основной LLM + голос + превью)
YANDEX_API_KEY=        # console.yandex.cloud → Сервисные аккаунты → API-ключ
YANDEX_FOLDER_ID=      # console.yandex.cloud → ID каталога (b1g...)
# Роль для сервисного аккаунта: ai.languageModels.user

# LLM fallback
GROQ_API_KEY=          # console.groq.com
GEMINI_API_KEY=        # aistudio.google.com → google-genai SDK

# Публикация
ZERNIO_API_KEY=        # zernio.com/dashboard → API Keys
ZERNIO_ACCOUNT_ID=     # zernio.com/dashboard → Social Accounts

# Медиа
PEXELS_API_KEY=        # www.pexels.com/api (видео + фото — один ключ)
UNSPLASH_ACCESS_KEY=   # unsplash.com/developers — опционально для photo-burst fallback

# Research Agent — Web search providers (любой 1+ необязателен; работает на DuckDuckGo без ключей)
TAVILY_API_KEY=        # tavily.com — 1000/мес бесплатно, лучшее качество сниппетов
BRAVE_API_KEY=         # brave.com/search/api — 2000/мес
SERPER_API_KEY=        # serper.dev — 2500/мес, Google API

# Reddit (Trend Scout)
REDDIT_CLIENT_ID=      # reddit.com/prefs/apps
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=content-factory/1.0
REDDIT_SUBREDDITS=psychology,selfimprovement,science

# Telegram (Notify Agent)
TELEGRAM_BOT_TOKEN=    # @BotFather → /newbot
TELEGRAM_CHAT_ID=      # ID чата для алертов

# БД
DB_PATH=pipeline.db

# Budget mode — только бесплатные провайдеры (см. раздел ниже)
BUDGET_MODE=free
DISABLED_PROVIDERS=    # точечно: kie,gemini,...
```

---

## Budget mode и провайдер-гейт (utils/providers.py)

`BUDGET_MODE=free` в .env переводит пайплайн на 100% бесплатный стек:
- **LLM:** Groq (llama-3.3-70b-versatile) → Cerebras (gpt-oss-120b) — Yandex пропускается
- **Голос:** edge-tts (Svetlana/Dariya/Dmitry; ниши маппятся через `EDGE_PERSONA_MAP`) → Silero (офлайн-резерв)
- **Видео/фото:** Pexels (+ Unsplash); Kling/KIE выключен
- **Субтитры:** Whisper local; **монтаж:** FFmpeg; **музыка:** локальная папка/FMA; **thumbnail:** Pixazo → Pillow A/B
- **Quality Scorer:** Gemini → Groq llama-4-scout (vision, бесплатный tier)

Платные провайдеры (`kie`, `elevenlabs`, `yandex_tts`, `yandex_llm`, `yandexart`, `dashscope`) гейтятся через `utils.providers.is_enabled()`. Runtime circuit breaker `mark_dead(provider)` выключает провайдера до конца процесса при 402 «Credits insufficient» / Gemini «limit: 0» — без спама ретраями. Любой новый вызов платного API обязан проверять `is_enabled()`.

Когда появится баланс — убрать `BUDGET_MODE=free` из .env, цепочки сами вернутся к платным провайдерам.

---

## Story-формат (--format story)

Серийные истории с накопительным лором, полностью бесплатно:
- **agents/story_agent.py** — библия мира в SQLite (`lore_worlds`, `lore_characters`, `lore_episodes`). Первый запуск ниши создаёт мир через LLM (сеттинг, тон, EN style_suffix, герой с детальным EN visual_prompt + фиксированный seed). Каждый эпизод пишется с контекстом последних 3 эпизодов, начинается с разрешения прошлого клиффхэнгера, заканчивается новым.
- **agents/image_agent.py** — сцены через Pollinations.ai FLUX (бесплатно, ключ POLLINATIONS_API_KEY опционален). Консистентность героя: один seed + дословный visual_prompt в каждом кадре + единый style_suffix.
- **Сборка** — `assemble_video(clips=<изображения>, ordered=True)`: сцены идут в нарративном порядке (без shuffle), каждая рендерится в photo-клип с Ken Burns.
- Пайплайн: мир → эпизод → safety → (голос ermil/Dmitry ∥ сцены ∥ музыка) → субтитры → сборка → QA → публикация → `save_episode` в лор.

---

## Платформы публикации
TikTok, Instagram Reels, YouTube Shorts, LinkedIn, X (Twitter)

---

## Важные решения и соглашения
- Все агенты общаются через JSON
- Состояние хранится в SQLite файле `pipeline.db`
- Каждый агент — отдельный Python модуль в `/agents/`
- Конфиги и API ключи в `.env` файле (не коммитить в git)
- Логи пишутся в `/logs/` с ротацией раз в неделю
- При ошибке агент бросает исключение наверх — Orchestrator делает retry 3x
- Telegram алерты при 3+ неудачных retry
- Gemini SDK: использовать `google-genai` (новый), не `google.generativeai` (deprecated)
- topic_filter.mark_published() вызывается Orchestrator'ом только после успешной публикации

---

## Структура проекта
```
content-factory/
├── CLAUDE.md              # этот файл — контекст проекта
├── .env                   # API ключи (не в git)
├── .env.example           # шаблон с комментариями
├── main.py                # точка входа, Master Orchestrator (--niche / --niches)
├── scheduler.py           # ✅ планировщик 3x/день + ежедневная сводка
├── pipeline.db            # SQLite база состояния (создаётся автоматически)
├── agents/
│   ├── trend_scout.py     # ✅ Google Trends + YouTube + Google News + habr + vc.ru
│   ├── idea_generator.py  # ✅ Groq → топ-3 идеи с hook/angle/hashtags
│   ├── topic_filter.py    # ✅ дедупликация SQLite + Gemini/Cerebras safety-check
│   ├── script_writer.py   # ✅ Groq → сценарий PAS, 140-165 слов, разговорный стиль
│   ├── fact_checker.py    # ✅ Groq → проверка фактов (люди/профессии, цифры, противоречия)
│   ├── voice_agent.py     # ✅ edge-tts → MP3
│   ├── music_agent.py     # ✅ Groq mood → FMA/local → фоновый трек
│   ├── visual_agent.py    # ✅ Pexels → stock footage клипы (словарь NICHE_KEYWORDS ниша→query)
│   ├── assembly_agent.py  # ✅ FFmpeg → Ken Burns + crossfade + gradient + amix
│   ├── subtitle_agent.py  # ✅ Whisper → ASS субтитры
│   ├── thumbnail_agent.py # ✅ Pixazo/Pillow → thumbnail.jpg
│   ├── publisher_agent.py # ✅ Zernio API → публикация (publishNow=True)
│   ├── notify_agent.py    # ✅ Telegram: карточка с превью + HTML-escape
│   ├── qa_agent.py        # ✅ ffprobe — проверка MP4 перед публикацией
│   ├── safety_agent.py    # ✅ Gemini/Cerebras — проверка политик платформ
│   └── analytics_agent.py # ✅ Zernio analytics → топ-3 по просмотрам
├── assets/
│   └── music/             # локальные треки: upbeat/, calm/, dramatic/
├── output/
│   ├── video/             # финальные MP4
│   ├── subtitles/         # ASS файлы субтитров
│   └── thumbnails/        # JPG превью
├── tests/
└── logs/
```

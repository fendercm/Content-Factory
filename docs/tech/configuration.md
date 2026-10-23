# Настройка

Все секреты и переключатели — в `.env` в корне проекта (в git не попадает, шаблон — `.env.example`).
Сериалы настраиваются файлами `config/series/<id>.json` ([series.md](series.md#конфиг-сериала--configseriesidjson)).

## Переменные окружения

### LLM и голос
| Переменная | Назначение |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio — основной бесплатный провайдер: LLM, TTS, vision. **Нужен VPN из РФ** |
| `GEMINI_MODEL`, `GEMINI_QUALITY_MODEL`, `GEMMA_MODEL` | модели tier fast / quality / последний резерв (по умолчанию алиасы `gemini-flash-lite-latest`, `gemini-flash-latest`) |
| `GEMINI_TTS` | `off` — не использовать Gemini TTS (ролики по нишам) |
| `GEMINI_TTS_MODEL` | список моделей TTS через запятую (переопределяет встроенную очередь) |
| `KIE_API_KEY` | KIE.ai — сценаристы premium, Nano Banana, Wan, Kling Avatar, загрузка файлов |
| `SERIES_WRITER_MODEL`, `SERIES_WRITER_FALLBACK` | сценаристы premium (по умолчанию `gemini:gemini-4-argon`, `codex:gpt-6-astra`) |
| `SERIES_CRITIC_MODEL`, `SERIES_CRITIC_FALLBACK` | критик сценариев, tier `critic` (по умолчанию `codex:gpt-6-astra`, `claude:claude-fable-5`) |
| `GROQ_API_KEY`, `GROQ_MODEL`, `GROQ_VISION_MODEL` | Groq — **выключен** решением 2026-10-21 (`DISABLED_PROVIDERS=groq`) |
| `NVIDIA_API_KEY`/`NVIDIA_MODEL`, `OPENROUTER_API_KEY`/`OPENROUTER_MODEL` | необязательные бесплатные резервы |
| `YANDEX_API_KEY`, `YANDEX_FOLDER_ID` | Yandex AI Studio / SpeechKit / YandexART (платно) |
| `CEREBRAS_API_KEY`/`CEREBRAS_MODEL`, `ELEVENLABS_API_KEY`, `DASHSCOPE_API_KEY` | платные резервы |
| `WHISPER_MODEL` | локальная модель Whisper (`base`; `initial_prompt` не использовать — галлюцинации) |

### Бюджет и провайдеры
| Переменная | Назначение |
|---|---|
| `BUDGET_MODE` | `free` — платные провайдеры выключены |
| `BUDGET_ALLOW` | платные провайдеры, разрешённые даже в `free`; для сериала и Студии — `kie_series` |
| `DISABLED_PROVIDERS` | выключить поимённо: `groq,kie,gemini,…` |

### Публикация и уведомления
| Переменная | Назначение |
|---|---|
| `ZERNIO_API_KEY` | Zernio — публикация и статистика |
| `ZERNIO_ACCOUNT_ID` | аккаунт по умолчанию |
| `ZERNIO_ACCOUNT_ID_<ID СЕРИАЛА>` | основной аккаунт сериала (например `ZERNIO_ACCOUNT_ID_KABINET7`) |
| `ZERNIO_ACCOUNT_ID_<ПЛОЩАДКА>` | аккаунты `also_publish`: `_TIKTOK`, `_INSTAGRAM`, `_YOUTUBE` |
| `TIKTOK_PRIVACY_LEVEL` | приватность TikTok-постов (по умолчанию `PUBLIC_TO_EVERYONE`) |
| `VK_USER_TOKEN`, `VK_GROUP_ID` | VK video.save (ключ с правом video выдаёт devsupport@corp.vk.com) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | алерты, карточки серий, ролики для ручной загрузки |

### Медиа и исследования (ролики по нишам)
`PEXELS_API_KEY`, `UNSPLASH_ACCESS_KEY`, `POLLINATIONS_API_KEY`, `PIXAZO_API_KEY`/`PIXAZO_BASE_URL`,
`FMA_API_KEY`, `YOUTUBE_API_KEY`, `TAVILY_API_KEY`, `BRAVE_API_KEY`, `SERPER_API_KEY`.
`YOUTUBE_API_KEY` (YouTube Data API v3) нужен и статистике: `analytics_agent` берёт из него просмотры, лайки и
комментарии роликов на YouTube. Без ключа — только Zernio, а у него статистика YouTube приходит с опозданием.

### Панель, планировщик, хранение
| Переменная | Назначение |
|---|---|
| `API_HOST`, `API_PORT`, `ADMIN_TOKEN`, `API_ALLOWED_HOSTS` | сервер панели ([admin_panel.md](admin_panel.md#безопасность)) |
| `SCHEDULER_MODE`, `SERIES_ID`, `SERIES_PLATFORM`, `SERIES_PUBLISH_TIME` | старый режим одного сериала; сейчас расписание задаётся в конфиге сериала |
| `SCHEDULER_NICHE`, `SCHEDULER_NICHES`, `SCHEDULER_PLATFORM` | ролики по нишам 3 раза в день |
| `DB_PATH` | база (по умолчанию `pipeline.db`) |
| `CLIPS_CACHE_MAX_GB`, `LOG_RETENTION_DAYS`, `OUTPUT_RETENTION_DAYS` | уборка диска (`utils/housekeeping.py`) |

> `BUDGET_ALLOW`, `ZERNIO_ACCOUNT_ID_<…>`, `SERIES_WRITER_*`, `TIKTOK_PRIVACY_LEVEL` пока не внесены в
> `.env.example` — при развёртывании с нуля добавьте их вручную.

## Гейт провайдеров — `utils/providers.py`

- `PAID_PROVIDERS`: `kie`, `kie_series`, `elevenlabs`, `yandex_tts`, `yandex_llm`, `yandexart`,
  `dashscope`, `cerebras`. `kie_series` — отдельный пропуск для сериала/Студии, чтобы разрешение не
  включало старые платные пути (Kling в `visual_agent`, ElevenLabs, Suno).
- `is_enabled(name)` — учитывает `BUDGET_MODE`, `BUDGET_ALLOW`, `DISABLED_PROVIDERS` и «мёртвых».
- `mark_dead(name, reason)` — выключить до конца процесса (402, «limit: 0», 401, исчерпанная квота).
- Любой новый вызов платного API обязан проверять `is_enabled()`.

## LLM-цепочка — `utils/llm_client.chat(messages, tier, json_mode, max_tokens, caller)`

```
premium: KIE Gemini 4 Argon → KIE GPT-6 Astra → как quality       (сценарии сериала и заказов)
quality: Yandex* → Gemini Flash → Groq✗ → Gemini Flash-Lite → NVIDIA → OpenRouter → Gemma → Cerebras*
fast:    Yandex* → Gemini Flash-Lite → Groq✗ → NVIDIA → OpenRouter → Gemma → Cerebras*
           * платные, пропускаются в BUDGET_MODE=free;  ✗ выключен
```

(`critic`: KIE GPT-6 Astra → KIE Claude Fable 5 → как quality.)

Отказы бывают трёх видов:

| Вид | Что это | Что делаем |
|---|---|---|
| постоянный | 401/403/404, 402, дневная квота («limit: 0», PerDay, tokens per day) | `mark_dead` — модель выключена до конца процесса |
| временный | 500/502/503/504/524, минутный 429, таймаут, обрыв соединения | следующий провайдер; модель можно повторить |
| прочий | 400, пустой ответ | следующий провайдер, без повтора |

**Повторные проходы.** Если отказала вся цепочка и среди причин есть временные, `chat()` ждёт и
проходит цепочку ещё раз — только по провайдерам с временным отказом в прошлом проходе (выключенные
и «прочие» не повторяются). Так ночная сборка сериала переживает минутную перегрузку Gemini
(2026-10-23 раскадровка упала на 503 + ConnectTimeout + Gemma 500, а через минуту всё работало).

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `LLM_TRANSIENT_RETRIES` | `2` | сколько повторных проходов; `0` — без повторов |
| `LLM_RETRY_DELAYS` | `20,60` | паузы перед проходами, с; если пауз меньше, чем проходов, повторяется последняя |

В худшем случае вызов ждёт лишнюю ~1 мин 20 с, потом `AllProvidersFailed` («проходов: 3»). В логе
видно, какой проход идёт (`[проход 2/3]`), у кого временный сбой и через сколько секунд повтор.

**Платные провайдеры не оплачиваем дважды.** KIE (`kie_series`), Yandex и Cerebras повторяются,
только если запрос точно не дошёл до генерации: ConnectTimeout, ошибка установки соединения
(DNS, отказ, прокси/VPN) или ответ 429/500/502/503/504. Таймаут чтения, обрыв посреди ответа и
Cloudflare 524 (KIE принял запрос, но не ответил за 100 с) значат, что модель могла отработать и
списать кредиты, — такую модель повторно не зовём, цепочка идёт к резерву и бесплатным провайдерам.

ID моделей устаревают за месяцы — перед сменой проверяйте живой список (`GET /v1beta/models` у Gemini).

## KIE — `utils/kie_client.py`

`create_task` → `wait_task` (опрос `recordInfo` каждые 8 с) → `download` (повторы, `.part`).
`run_task(model, input, timeout_s, retries=1, pause_s=30)` повторяет задачу при временных сбоях
(5xx, «internal error / try again», обрыв сети); не повторяет 402, выключенный KIE и таймаут задачи.
`upload_file` — ссылка живёт 24 ч. `chat(model_spec, …)` — LLM через KIE (стриминг). Расходы пишутся в
`costs` (`kie:<model>`). Баланс — `balance()` (в кредитах, 200 кр = $1).

Лимит трат процесса: `set_spend_limit(usd)` обнуляет счётчик `spent_usd()` (те же суммы, что идут в
`costs`); когда он дошёл до лимита, `chat` и `create_task` не отправляют запрос и бросают `KieLimitError`
(`limit_reached()`, `check_spend_limit(what)`). По умолчанию лимита нет; ставит его `script_lab.py --max-usd`.
Оценка эпизода сериала и сверка с балансом — `utils/series_budget.py` ([series.md](series.md#себестоимость-kie-200-кр--1)).

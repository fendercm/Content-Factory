# Данные

## База `pipeline.db` (SQLite)

Путь — `DB_PATH` (по умолчанию `pipeline.db` в корне). Таблицы создаются кодом при первом обращении
(`CREATE TABLE IF NOT EXISTS`, недостающие колонки добавляются `ALTER TABLE`). Время — ISO 8601 UTC.

### Запуски и журнал

| Таблица | Колонки | Кто пишет / читает |
|---|---|---|
| `pipeline_state` | `run_id, step, status, payload (JSON), error, created_at` | каждый шаг пайплайна: `running` → `success` / `failed_attempt_N` / `failed`; итог — строка `step='pipeline'` (`completed`/`failed`, в payload `status: queued\|published`, `episode`, `cost_usd`). Панель строит по ней этапы сборки |
| `agent_messages` | `timestamp, sender, receiver, message, run_id` | `utils.agent_logger.log_message` — короткие «что сейчас происходит», панель показывает последнее |
| `error_log` | `run_id, step, error, attempt, created_at` | ошибки шагов (секреты замаскированы) |
| `costs` | `run_id, agent, provider, operation, units, unit_cost_usd, total_usd, created_at` | `utils.cost_tracker.log_cost`; KIE пишет `kie:<model>` по `creditsConsumed` |

`run_id`: `YYYYMMDD_HHMMSS_<формат или ниша>`; у сериала — `…_series_<id>`, выпуск — `…_series_<id>_publish`.

### Сериал

| Таблица | Колонки | Назначение |
|---|---|---|
| `anthology_episodes` | `series_id, episode_no, title, client, psych_concept, summary, run_id, post_id, created_at, status, video_path, publish_title, description, source_topic, published_at` | очередь и история серий; `status`: `ready` / `published` / `rejected`. Номер серии уникален среди не снятых |
| `lab_pitches` | `series_id, batch, data (JSON), vote (−1/0/1), used, created_at` | завязки лаборатории и отметки автора |

### Ролики по нишам, story, аналитика

| Таблица | Назначение |
|---|---|
| `topics_used (topic, published_at)` | дедупликация тем за 30 дней; пишется только после реальной публикации |
| `performance (post_id, platform, title, published_at, views, likes, comments, niche, hook, fetched_at)` | статистика Zernio (`analytics_agent`, upsert по `post_id`); панель сопоставляет с `anthology_episodes.post_id` |
| `series`, `series_episodes` | многосерийность внутри ниши (`series_agent`) |
| `lore_worlds`, `lore_characters`, `lore_episodes` | библия мира story-формата |

## Файлы

| Путь | Содержимое | Кто создаёт |
|---|---|---|
| `config/series/<id>.json` | конфиг сериала | вручную / панель (`PUT /api/series`) |
| `config/series/scripts/<id>_<slug>.json` | сценарий для производства `{title, case, lines}` | панель, вручную |
| `assets/series/<id>/` | `*_ref.jpg` — референс героини; `candidates/` — варианты портрета | `scripts/series_setup.py` |
| `assets/music/{calm,upbeat,dramatic}/` | локальная музыка | `scripts/download_music.py` |
| `output/series/<run_id>/` | `dialogue.wav`, `shot_NN.jpg/.mp4`, `client_ref.jpg`, `lip_NN.mp3/.mp4`, `subtitles.ass` | сборка эпизода; используется `--resume` |
| `output/video/<run_id>_epNNN.mp4` | готовая серия; `…_segments/` — куски монтажа, `video_only.mp4`, `mix.wav` | `series_assembly` |
| `output/orders/<id>/` | `order.json`, `files/`, `build/`, `<id>.mp4` | Студия |
| `output/script_lab/<YYYYMMDD_HHMM>.{md,json}` + `_key.json` | варианты лаборатории и ключ «кто писал» | `script_lab.py` |
| `output/{video,subtitles,thumbnails,music}/` | ролики по нишам | исходный пайплайн |
| `logs/*.log` | логи пайплайна | все процессы |
| `logs/jobs/<id>.log`, `logs/jobs/jobs.json` | вывод и список задач панели | `api_server` |
| `logs/scheduler.heartbeat` | пульс планировщика (обновляется каждые 30 с) | `scheduler.py` |

## Резервное копирование

Ценное и невосстановимое: `pipeline.db`, `config/series/`, `assets/series/`, `.env`.
`output/` можно чистить (`OUTPUT_RETENTION_DAYS`), но папки запусков нужны для доделки эпизодов,
а ролики в очереди — для выпуска.

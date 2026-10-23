# Справочник модулей

Коротко о каждом модуле: роль и главный вход. Подробности — в docstring модуля.

## Оркестрация

| Модуль | Роль | API / запуск |
|---|---|---|
| `main.py` | оркестратор: ролики (`run_pipeline`), story (`run_story_pipeline`), сериал (`run_series_pipeline`, `publish_next_series_episode`); повторы шагов, `pipeline_state`, алерты | CLI: `--niche/--niches`, `--platform`, `--format clips\|story\|series`, `--dry-run`, `--custom-topic`, `--series`, `--prepare N`, `--publish-next`, `--script FILE`, `--resume RUN_ID` |
| `scheduler.py` | расписание: выпуск сериалов по `schedule` их конфигов, ролики 3×/день, сводка 21:00, аналитика 08:30, уборка 04:00; пульс | `py scheduler.py` |
| `scripts/order_run.py` | шаг заказа Студии | `--order ID --step script\|build` |
| `utils/api_server.py` | сервер панели, фоновые задачи | [admin_panel.md](admin_panel.md) |

## Сценарий и содержание

| Модуль | Роль | Главное |
|---|---|---|
| `agents/anthology_agent.py` | сериал: конфиг, кейсы, завязки, сценарий, проверки, раскадровка, очередь | `load_series`, `propose_cases`, `propose_pitches`, `write_episode`, `write_from_pitch`, `plan_shots`, `assign_shots`, `queue_episode`, `next_ready`, `set_episode_status`, `hero_fields` |
| `agents/order_agent.py` | Студия: заказ, сценарий, раскадровка, сборка | `new_order`, `write_script`, `normalize_script`, `plan_shots`, `build`, `estimate_usd` |
| `agents/script_writer.py` | сценарий ролика диктора по фактам | `write_script`, `fix_script`, `improve_hook` |
| `agents/story_agent.py` | лор и эпизоды story-формата | `get_or_create_world`, `write_episode` |
| `agents/series_agent.py` | многосерийная тема внутри ниши | `create_series`, `get_next_episode` |
| `agents/trend_scout.py` | тренды без LLM | `run(niche)` |
| `agents/idea_generator.py` | идеи по трендам | `run(topics, niche)` |
| `agents/topic_filter.py` | дубли, ниша, safety (fail-closed) | `run`, `mark_published` |
| `agents/research_agent.py` | факты с уровнем доверия | `research(topic, query)` |
| `agents/fact_checker.py` | сверка сценария с фактами (fail-closed) | `check(script, facts)` |
| `agents/safety_agent.py` | стоп-категории и правила платформ (fail-closed) | `check(script, title, hashtags, platform)` |
| `agents/comment_writer.py` | закреплённый комментарий к ролику | `write_pinned_comment` |

## Голос, картинка, монтаж

| Модуль | Роль | Главное |
|---|---|---|
| `agents/dialogue_voice.py` | озвучка по ролям (сериал, Студия): multi-speaker Gemini TTS, пометки → голос, Whisper-тайминги, проверка дублей | `synthesize(series, episode, out_dir)`; внутренние `_render_dialogue`, `_render_lines` |
| `agents/voice_agent.py` | голос диктора роликов: Gemini TTS → edge-tts → Silero | `generate_voice`, `voice_for_niche` |
| `agents/scene_agent.py` | кадры Nano Banana 2 с референсами, анимация Kling/Wan | `render(series, plan, out_dir)` |
| `agents/lipsync_agent.py` | говорящие лица Kling AI Avatar на ключевых репликах | `key_lines`, `render`, `apply` |
| `agents/visual_agent.py` | сток Pexels/Unsplash для роликов | `get_visuals`, `get_photos` |
| `agents/image_agent.py` | картинки Pollinations FLUX (story) | `generate_scenes` |
| `agents/entity_visuals.py` | фото классиков и институтов из Wikipedia | `run` |
| `agents/counter_overlay.py` | анимированные числа | `find_counters`, `render_counter_clip` |
| `agents/music_agent.py` | фон по настроению: Suno (KIE, вне бюджета) → локально → FMA | `get_music(text, mood)` |
| `agents/subtitle_agent.py` | ASS-субтитры: ролики (`generate_subtitles`) и диалоги по ролям (`write_dialogue_ass`, `play_res`) | выравнивание `_align_to_script` |
| `agents/series_assembly.py` | монтаж серии/заказа: таймлайн по репликам, клипы/зум, звук отдельным проходом, логотип, размер кадра | `build_timeline`, `assemble` |
| `agents/assembly_agent.py` | монтаж роликов по нишам (hybrid timeline) | `assemble_video` |
| `agents/thumbnail_agent.py` | превью YandexART → Pixazo → Pillow, A/B | `generate_thumbnail`, `ab_test_thumbnails` |

## Проверка и выпуск

| Модуль | Роль | Главное |
|---|---|---|
| `agents/series_qa.py` | автопроверка готовой серии без LLM | `check(video, dialogue, subtitles, timeline, shots)` → `{passed, problems, metrics}` |
| `agents/qa_agent.py` | ffprobe: длительность, формат, потоки | `check(video, platform)` |
| `agents/quality_scorer.py` | оценка кадров vision-моделью (1–10) | `score_video` |
| `agents/publisher_agent.py` | Zernio: presign → upload → один `POST /posts`; метки ИИ | `publish_video(..., ai_generated)` |
| `agents/vk_publisher.py` | VK video.save в сообщество | `is_configured`, `publish_video` |
| `agents/notify_agent.py` | Telegram: алерты, карточки, ролики для ручной загрузки | `send`, `send_video`, `send_manual_upload`, `alert_error`, `alert_success`, `alert_queued` |
| `agents/analytics_agent.py` | статистика → `performance`: YouTube Data API `videos.list` для YouTube, Zernio `GET /analytics?postId=` для остальных | `run(days)`; `py agents/analytics_agent.py --days 7` |

## Инфраструктура — `utils/`

| Модуль | Роль |
|---|---|
| `llm_client.py` | LLM-цепочка с автопереключением (`chat`, tiers `fast`/`quality`/`premium`) |
| `kie_client.py` | KIE: задачи, повторы при временных сбоях, загрузка, скачивание, LLM, баланс |
| `providers.py` | гейт платных провайдеров, отключение на лету |
| `ffmpeg.py` | запуск FFmpeg с таймаутом и stderr в ошибке |
| `download.py` | атомарные загрузки (`.part` → rename) |
| `cost_tracker.py` | учёт расходов (`costs`) |
| `agent_logger.py` | сообщения агентов (`agent_messages`) |
| `log_redact.py` | маскирование секретов в логах и ошибках |
| `housekeeping.py` | уборка кэша, логов, старых результатов |

## Служебные скрипты — `scripts/`

| Скрипт | Назначение |
|---|---|
| `script_lab.py` | лаборатория сценариев (кейсы, идея, завязки, из завязок) |
| `series_queue.py` | очередь: `list`, `reject N`, `restore N` |
| `series_setup.py` | портреты героини → `assets/series/<id>/candidates/` |
| `order_run.py` | шаги заказа Студии |
| `model_bakeoff.py`, `animation_bakeoff.py` | слепые сравнения LLM-сценаристов и видеомоделей |
| `download_music.py` | наполнение локальной музыкальной библиотеки |

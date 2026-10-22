# Ролики по нишам и story-формат

Исходный продукт проекта — короткие ролики «диктор + сток» по тематике (нише), три раза в день.
Сейчас основной фокус — сериал, но пайплайн рабочий и запускается так же, через `main.py`.

```bash
py main.py --niche "психология" --dry-run                  # без публикации
py main.py --niche "бизнес" --platform youtube
py main.py --niches "психология,бизнес,здоровье"           # каждая ниша — отдельный процесс
py main.py --niche "тёмное фэнтези" --format story --dry-run
```

Платформы: `tiktok` · `instagram` · `youtube` · `linkedin` · `twitter`.

## Пайплайн — `main.run_pipeline`

| # | Шаг | Модуль | Суть |
|---|---|---|---|
| 1 | trend_scout | `trend_scout` | топ-5 тем: Google Trends, YouTube Trending, Google News RSS, Habr, vc.ru; без LLM |
| 2 | idea_generator | `idea_generator` | 3 идеи разных форматов подачи (миф, скрытая причина, ошибка, эксперимент, сравнение) + `research_query` |
| 3 | topic_filter | `topic_filter` | дубли за 30 дней (`topics_used`), соответствие нише, safety; fail-closed |
| 4 | research_agent | `research_agent` | факты с метками доверия: Wikipedia RU/EN, Semantic Scholar, Google News, веб-поиск Tavily → Brave → Serper → DuckDuckGo; LLM-фильтр релевантности |
| 5 | script_writer | `script_writer` | сценарий по пресету длительности (45 с ≈ 120–135 слов, минимум 120): HOOK → SETUP → 2 контраста → вопрос; цифры только из фактов |
| 6 | fact_checker | `fact_checker` | сверка с фактами; при замечаниях — одна точечная правка (`fix_script`), иначе следующая идея |
| 7 | safety_agent | `safety_agent` | политика, медсоветы, 18+, правила платформы |
| 8 | voice_agent | `voice_agent` | Gemini TTS (персоны ниш) → edge-tts → Silero; платные ElevenLabs/Yandex — вне бюджетного режима |
| 9 | music_agent | `music_agent` | настроение (upbeat/calm/dramatic) → локальная библиотека / FMA |
| 10 | visual_agent | `visual_agent.get_visuals` | ~25 клипов Pexels по блокам сценария, только вертикальные |
| 11 | subtitle_agent | `subtitle_agent.generate_subtitles` | Whisper-тайминги, слова из сценария, ASS Impact 96pt |
| 12 | entity_visuals | `entity_visuals` | фото учёных/институтов из Wikipedia на момент упоминания |
| 13 | get_photos | `visual_agent.get_photos` | ~17 фото Pexels/Unsplash, perceptual-hash дедуп |
| 14 | counter_overlays | `counter_overlay` | анимированные счётчики для чисел в сценарии |
| 15 | assembly_agent | `assembly_agent` | hybrid timeline: 50/50 фото/клипы, оверлеи, photo burst, переходы, цвет, loudnorm + sidechain |
| 16 | thumbnail_agent | `thumbnail_agent` | превью: YandexART → Pixazo → Pillow (A/B) |
| 17 | quality gate + qa_agent | `quality_scorer`, `qa_agent` | vision-оценка кадров; ffprobe (длительность, формат, потоки) |
| 18 | publisher_agent | `publisher_agent` | Zernio, `publishNow`, одна попытка |
| 19 | notify | `notify_agent` | карточка в Telegram с кадром из видео |

`--custom-topic "тема"` (только с `--niche`) пропускает шаги 1–3. `--format` — `clips` (по умолчанию),
`story`, `series`. После успешной публикации тема
помечается в `topics_used` (`topic_filter.mark_published`).

Многосерийность внутри ниши — `series_agent` (тема на N частей, «Часть k из N»); не путать с
сериалом-антологией ([series.md](series.md)).

## Story-формат — `main.run_story_pipeline`

Серийная история с накопительным лором, бесплатно:
- `story_agent` — библия мира в SQLite (`lore_worlds`, `lore_characters`, `lore_episodes`): первый
  запуск ниши создаёт мир (сеттинг, тон, стиль, герой с фиксированным seed); каждый эпизод — с контекстом
  трёх прошлых, начинается разрешением клиффхэнгера и заканчивается новым;
- `image_agent` — сцены через Pollinations FLUX с одним seed и дословным описанием героя;
- сборка `assemble_video(clips=<картинки>, ordered=True)` — сцены по порядку, Ken Burns.

## Вспомогательные скрипты в корне

| Файл | Назначение |
|---|---|
| `batch_dry_run.py` | серия dry-run роликов по разным нишам |
| `night_runner.py` | ночной автозапуск каждые 60 мин с ротацией ниш |
| `night_finalize.py`, `morning_summary.py` | финализация ночных рендеров и отчёт в Telegram |
| `test_pipeline.py`, `read_script.py` | ручные отладочные скрипты |

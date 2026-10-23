# Тесты

## Запуск

```bash
pip install -r requirements.txt -r requirements-dev.txt
py -X utf8 -m pytest -q                    # все тесты
py -X utf8 -m pytest -q -m "not heavy"     # без долгих (librosa/Whisper/реальный FFmpeg) — так в CI
py -X utf8 -m coverage run -m pytest -q
py -X utf8 -m coverage report --omit="tests/*" --sort=cover
```

Тесты в сеть не ходят: `tests/conftest.py` обнуляет ключи Telegram/Zernio/VK, а `requests`, `subprocess.run`,
KIE и Telegram подменены моками. Маркер `heavy` (`pytest.ini`) — для тестов дольше нескольких секунд.

CI: `.github/workflows/tests.yml` — Ubuntu и Windows, Python 3.13, ffmpeg, `pytest -q -m "not heavy"`.

## Покрытие (2026-10-23)

Базовое до новых тестов: 194 теста, 49 % по `agents/` и `utils/`; `scheduler.py` ни один тест не импортировал.
Слабее всего: visual_agent 16 %, voice_agent 22 %, music_agent 27 %, scene_agent 27 %, quality_scorer 28 %,
kie_client 34 %, assembly_agent 38 %, order_agent 39 %, notify_agent 43 %, series_assembly 44 %,
series_qa 66 %, publisher_agent 76 %, analytics_agent 78 %, vk_publisher 93 %.

Добавлено: `test_publisher_flow.py`, `test_notify_agent.py`, `test_scheduler.py` (+86 тестов).
Не успели: дополнительные тесты для `series_qa`, `analytics_agent`, `vk_publisher`.

## Найденные проблемы (исходники не менялись)

1. **Пульс планировщика устаревает во время выпуска.** `scheduler.main()` выполняет задачи синхронно
   (`schedule.run_pending()`, `catch_up_series()` при старте), а `job_series_publish` ждёт `main.py` до часа.
   Всё это время `logs/scheduler.heartbeat` не обновляется, через 90–120 с панель считает планировщик мёртвым
   и `_autostart_scheduler()` при перезапуске панели запустит второй экземпляр — возможен двойной выпуск.
   Фикс: обновлять пульс в отдельном потоке-демоне (раз в 30 с) или запускать задачи в потоках.
2. **Неверное время в конфиге роняет планировщик.** `schedule.time = "7pm"` → `ScheduleValueError` в
   `sync_series_jobs()` внутри бесконечного цикла, процесс завершается; `catch_up_series` упадёт на `int()`.
   Фикс: проверять формат `HH:MM` в `series_schedules()` и пропускать сериал с предупреждением.
   Тест: `test_sync_survives_bad_time_in_config` (xfail).
3. **Конфиг сериала без `id` роняет `series_schedules()`** (`KeyError` не входит в `(OSError, ValueError)`).
   Фикс: `cfg.get("id") or path.stem`, либо ловить `KeyError`. Тест: `test_series_schedules_skips_config_without_id` (xfail).
4. **`notify_agent.send_video` падает, если файла нет**: `os.path.getsize` стоит до `try`, поэтому
   `send_manual_upload` с отсутствующим роликом бросает `FileNotFoundError` вместо `{"ok": False}`.
   Фикс: проверить `os.path.exists` и вернуть ошибку/текстовое сообщение.
5. **`notify_agent._esc(0)` возвращает пустую строку** (проверка `if text`). Фикс: `if text is None`.
6. `send_photo` при ошибке шлёт текст с уровнем `success` даже из `alert_queued` (ожидается `info`) — мелочь.
7. `scheduler.has_ready/published_today` берут БД по умолчанию из `_ROOT/pipeline.db`, а `job_daily_digest`
   — `pipeline.db` относительно текущей папки. Фикс: одна функция пути к БД.

## Трудно тестировать

- `import scheduler` подменяет `sys.stdout`, создаёт `logs/` в текущей папке и настраивает root-логгер;
  тест импортирует его в изоляции (фикстура `sched`). Фикс: перенести эту настройку в `main()`.
- `main.py` тестируется только подпроцессами с ручным обнулением ключей (в Windows пустая переменная
  окружения не наследуется). Фикс: выделить CLI-разбор и шаги в импортируемые функции без побочных эффектов.

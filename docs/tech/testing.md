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

## Покрытие

Базовое (2026-10-23), до новых тестов: 194 теста, 49 % по `agents/` и `utils/`; `scheduler.py` ни один тест не импортировал.
Слабее всего: visual_agent 16 %, voice_agent 22 %, music_agent 27 %, scene_agent 27 %, quality_scorer 28 %,
kie_client 34 %, assembly_agent 38 %, order_agent 39 %, notify_agent 43 %, series_assembly 44 %,
series_qa 66 %, publisher_agent 76 %, analytics_agent 78 %, vk_publisher 93 %.

После (2026-10-24): 327 тестов + 2 xfail, 53 % (с `scheduler.py` в отчёте).

| Модуль | Было | Стало | Файл с тестами |
|---|---|---|---|
| `agents/publisher_agent.py` | 76 % | 94 % | `test_publisher_flow.py` |
| `agents/notify_agent.py` | 43 % | 95 % | `test_notify_agent.py` |
| `scheduler.py` | — | 95 % | `test_scheduler.py` |
| `agents/series_qa.py` | 66 % | 100 % | `test_series_qa_checks.py` |
| `agents/analytics_agent.py` | 78 % | 96 % | `test_analytics_run.py` |
| `agents/vk_publisher.py` | 93 % | 100 % | `test_vk_publisher_edges.py` |

Непокрытые строки — в основном блоки `if __name__ == "__main__"`. Следующие кандидаты: `kie_client`
(платные вызовы — только на моках), `series_assembly`, `cost_tracker`, `housekeeping`.

## Найденные проблемы (исходники не менялись)

1. ✅ *Исправлено 2026-10-24: пульс в потоке `start_heartbeat`.* **Пульс планировщика устаревает во время выпуска.** `scheduler.main()` выполняет задачи синхронно
   (`schedule.run_pending()`, `catch_up_series()` при старте), а `job_series_publish` ждёт `main.py` до часа.
   Всё это время `logs/scheduler.heartbeat` не обновляется, через 90–120 с панель считает планировщик мёртвым
   и `_autostart_scheduler()` при перезапуске панели запустит второй экземпляр — возможен двойной выпуск.
   Фикс: обновлять пульс в отдельном потоке-демоне (раз в 30 с) или запускать задачи в потоках.
2. ✅ *Исправлено 2026-10-24: `series_schedules` пропускает сериал с неверным `ЧЧ:ММ`.* **Неверное время в конфиге роняет планировщик.** `schedule.time = "7pm"` → `ScheduleValueError` в
   `sync_series_jobs()` внутри бесконечного цикла, процесс завершается; `catch_up_series` упадёт на `int()`.
   Фикс: проверять формат `HH:MM` в `series_schedules()` и пропускать сериал с предупреждением.
   Тест: `test_sync_survives_bad_time_in_config` (был xfail, теперь обычный тест).
3. ✅ *Исправлено 2026-10-24.* **Конфиг сериала без `id` роняет `series_schedules()`** (`KeyError` не входит в `(OSError, ValueError)`).
   Фикс: `cfg.get("id") or path.stem`, либо ловить `KeyError`. Тест: `test_series_schedules_skips_config_without_id` (был xfail, теперь обычный тест).
4. ✅ *Исправлено 2026-10-24: `{"ok": False}` вместо исключения.* **`notify_agent.send_video` падает, если файла нет**: `os.path.getsize` стоит до `try`, поэтому
   `send_manual_upload` с отсутствующим роликом бросает `FileNotFoundError` вместо `{"ok": False}`.
   Фикс: проверить `os.path.exists` и вернуть ошибку/текстовое сообщение.
5. ✅ *Исправлено 2026-10-24.* **`notify_agent._esc(0)` возвращает пустую строку** (проверка `if text`). Фикс: `if text is None`.
6. `send_photo` при ошибке шлёт текст с уровнем `success` даже из `alert_queued` (ожидается `info`) — мелочь.
7. `scheduler.has_ready/published_today` берут БД по умолчанию из `_ROOT/pipeline.db`, а `job_daily_digest`
   — `pipeline.db` относительно текущей папки. Фикс: одна функция пути к БД.
8. **VK: HTTP 5xx при загрузке файла не повторяется** — `_upload` ретраит только обрыв и таймаут, а
   `raise_for_status()` на 502/503 сразу роняет публикацию (video.save уже создал пустую запись видео).
   Фикс: ретраить `HTTPError` с кодом ≥ 500, как в `publisher_agent._with_retries`.
9. **Аналитика: `content: null` у поста роняет весь сбор** — `post.get("content", "")[:80]` даёт `TypeError`
   на `None`. Фикс: `(post.get("content") or "")[:80]`. Там же: при не-HTTP ошибке `_fetch_posts`
   (обрыв соединения) соединение с SQLite не закрывается — `try/finally` вокруг всего `run()`.

## Трудно тестировать

- `import scheduler` подменяет `sys.stdout`, создаёт `logs/` в текущей папке и настраивает root-логгер;
  тест импортирует его в изоляции (фикстура `sched`). Фикс: перенести эту настройку в `main()`.
- `main.py` тестируется только подпроцессами с ручным обнулением ключей (в Windows пустая переменная
  окружения не наследуется). Фикс: выделить CLI-разбор и шаги в импортируемые функции без побочных эффектов.

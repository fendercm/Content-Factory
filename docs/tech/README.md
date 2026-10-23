# Content Factory — техническая документация

Content Factory — контент-завод на Python: ИИ пишет сценарии, озвучивает, рисует кадры, монтирует
вертикальные ролики и публикует их в соцсети. Всё управляется из локальной веб-панели.

Состояние на 2026-10-23. Три продукта на общем движке:

| Продукт | Что делает | Где описан |
|---|---|---|
| **Сериал** («Кабинет 7») | Антология: психолог Вера и новый клиент в каждой серии. Сценарий → голоса по ролям → кадры с постоянной героиней → липсинк → монтаж → автопроверка → очередь → публикация | [series.md](series.md) |
| **Студия** (видео на заказ) | Бриф клиента + его фото → сценарий → ролик 9:16 или 16:9 для скачивания. Не публикуется | [studio.md](studio.md) |
| **Ролики по нишам** (исходный пайплайн) | Тренды → идея → факты → сценарий диктора → стоковое видео → ролик. Плюс story-формат | [shorts.md](shorts.md) |

## Карта документации

- [architecture.md](architecture.md) — устройство: слои, процессы, потоки данных, принципы
- [series.md](series.md) — сериал: конфиг, лаборатория сценариев, сборка, QA, очередь, публикация
- [studio.md](studio.md) — заказы: бриф, сценарий, сборка, файлы клиента
- [shorts.md](shorts.md) — исходный пайплайн по нишам и story-формат
- [admin_panel.md](admin_panel.md) — веб-панель: страницы, фоновые задачи, REST API, безопасность
- [configuration.md](configuration.md) — `.env`, бюджетный режим, провайдеры, LLM-цепочка, конфиг сериала
- [data.md](data.md) — база `pipeline.db` и файлы на диске
- [modules.md](modules.md) — справочник модулей
- [operations.md](operations.md) — запуск, расписание, логи, тесты, типовые сбои, расходы

## Быстрый старт

```bash
pip install -r requirements.txt
```

1. Скопируйте `.env.example` в `.env` и заполните ключи (минимум для сериала: `GEMINI_API_KEY`,
   `KIE_API_KEY`, `BUDGET_ALLOW=kie_series`, `ZERNIO_API_KEY`, `TELEGRAM_*`). См. [configuration.md](configuration.md).
2. Установите FFmpeg (≥ 6, проверено на 9.0) и убедитесь, что `ffmpeg`/`ffprobe` в `PATH`.
3. Запустите панель: `start.bat` или

```bash
py -X utf8 utils/api_server.py
```

4. Откройте http://localhost:5000 — дальше всё делается из панели ([admin_panel.md](admin_panel.md)).

Из командной строки то же самое:

```bash
py main.py --format series --series kabinet7 --niche "психология" --prepare 1
```

```bash
py main.py --format series --series kabinet7 --niche "психология" --publish-next
```

## Требования

- Windows 10/11 (основная среда; код кроссплатформенный, но проверяется на Windows), Python 3.13
- FFmpeg/ffprobe в `PATH`
- Доступ к Google AI (Gemini) — из РФ нужен VPN; без него не работают сценарии, озвучка и проверки
  (см. [operations.md](operations.md#типовые-сбои))
- Python-зависимости из `requirements.txt`: openai-whisper (локально, модель `base`), numpy, librosa,
  Flask, requests, schedule, edge-tts, python-dotenv

## Главные соглашения

- Агенты — отдельные модули в `agents/`, общаются словарями/JSON; состояние — SQLite `pipeline.db`.
- Ключи только в `.env` (не в git); в логах, ошибках и Telegram секреты маскируются (`utils/log_redact.py`).
- Гейты качества fail-closed: если проверка (safety, факты, QA) не смогла отработать — контент не идёт дальше.
- Платные провайдеры вызываются только через гейт `utils.providers.is_enabled()`.
- Публикация — ровно одна попытка (повтор создал бы дубль поста).

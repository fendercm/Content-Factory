# Панель управления

`admin_panel.html` (одна страница, vanilla JS, тёмная тема, работает на телефоне) +
`utils/api_server.py` (Flask). Запуск — `start.bat` или `py -X utf8 utils/api_server.py`,
адрес http://localhost:5000. Панель опрашивает API каждые 5 с; поля ввода при опросе не перерисовываются.

## Страницы

| Страница | Что есть |
|---|---|
| 🏠 Обзор | счётчики сериала (в очереди, вышло, расходы за 7 дней, баланс KIE), этапы текущей сборки с длительностями, расписание выпуска и кнопка запуска планировщика, последние ошибки |
| 🎥 Студия | заказы: бриф, параметры, файлы клиента, сценарий с персонажами, сборка, плеер, «Скачать MP4» ([studio.md](studio.md)) |
| 🎬 Эпизоды | «Собрать N в очередь», «Выпустить следующий сейчас» (с подтверждением), таблица серий с плеером и «Снять/Вернуть», последние сборки с «Доделать» для упавших |
| ✍️ Сценарии | завязки с 👍/👎, лаборатория (опционально по идее), результаты вариантов («В производство», «В редактор»), сценарии для производства, редактор сценария серии |
| 📈 Аналитика | просмотры/лайки/комментарии по сериям (YouTube и TikTok через Zernio), цена серии и цена 1000 просмотров, «Обновить из соцсетей» |
| 🗂 Сериалы | карточки сериалов, настройки (героиня, линия сезона, режиссура озвучки, слова, анимация, липсинк, площадки, дисклеймер), портреты героини и выбор референса, расписание, «Новый сериал» |
| ⚙️ Задачи | фоновые задачи со статусом, журнал (хвост лога, ошибки подсвечены), «Стоп» |
| 🖥 Журнал | последние строки логов `logs/*.log` с уровнями |

В шапке — выбор сериала, баланс KIE, статус планировщика (по пульсу), связь с сервером.

## Фоновые задачи

`POST /api/jobs {type, series?, ...}` запускает отдельный процесс; вывод — `logs/jobs/<id>.log`,
список — `logs/jobs/jobs.json` (после перезапуска сервера незавершённые помечаются `lost`).
Одна задача одного типа на сериал/заказ одновременно (кроме `lab`) — иначе 409.

| type | Команда | Параметры |
|---|---|---|
| `prepare` | `main.py --format series --prepare N` | `n` ≤ 10, `platform` |
| `script` | `main.py … --script <file>` | `file` из `config/series/scripts/` |
| `resume` | `main.py … --resume RUN_ID [--script]` | `run_id`, `file?` |
| `publish_next` | `main.py … --publish-next` | **`confirm: true` обязателен** |
| `lab` | `scripts/script_lab.py` | `n`, `same`, `idea?`, `pitch_ids?` |
| `pitches` | `script_lab.py --pitches` | `n` 3–20, `idea?` |
| `heroine` | `scripts/series_setup.py <id>` | — |
| `analytics` | `agents/analytics_agent.py --days N` | `days` |
| `scheduler` | `scheduler.py` | — |
| `order_script`, `order_build` | `scripts/order_run.py --order ID --step script\|build` | `order` |

## REST API

Все ответы — JSON, ошибки — `{"error": "..."}` с кодом 4xx.

### Общее
| Метод | Путь | Описание |
|---|---|---|
| GET | `/` | панель |
| GET | `/api/overview` | сериалы со счётчиками, расходы за 7 дней, `kie_balance` (обновляется в фоне раз в минуту), `scheduler_alive`, `running_jobs`, `errors` |
| GET | `/api/logs?lines=` | `[{text, level: ""\|warn\|err}]` |
| GET/POST | `/api/jobs` | список (50 последних) / запуск |
| GET | `/api/jobs/<id>/log?lines=` | `{job, lines}` |
| POST | `/api/jobs/<id>/stop` | остановка процесса |

### Сериалы и эпизоды
| Метод | Путь | Описание |
|---|---|---|
| GET/POST | `/api/series` | список / создание по шаблону `kabinet7` (обязательны `name`, `heroine.{name,role,profession,setting,visual_en}`) |
| GET/PUT | `/api/series/<id>` | `{config, schedule, images, scripts}` / правка разрешённых полей и `schedule` |
| GET | `/api/series/<id>/heroine/<file>` | портрет; POST `/heroine/pick {file}` — сделать кандидата референсом |
| GET | `/api/series/<id>/episodes` | очередь и вышедшие (`has_video`) |
| POST | `/api/series/<id>/episodes/<row>` | `{action: reject\|restore}` |
| GET | `/api/video/<row>` | ролик эпизода (только файлы внутри `output/`, поддержка Range) |
| GET | `/api/series/<id>/builds` | текущая сборка с этапами + 10 последних |
| GET | `/api/analytics?series=&days=` | `{episodes, totals, kie_balance}` |

### Сценарии
| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/pitches?series=` | завязки; POST `/api/pitches/<id> {vote: 1\|0\|-1}` |
| GET | `/api/lab`, `/api/lab/<stamp>` | запуски лаборатории / варианты с моделью-автором |
| GET | `/api/scripts?series=`, `/api/scripts/<file>` | сценарии для производства |
| POST | `/api/scripts` | `{series, title, case, lines \| text}`; `text` — строки `РОЛЬ: [эмоция] реплика`; роли проверяются |

### Заказы (Студия)
| Метод | Путь | Описание |
|---|---|---|
| GET/POST | `/api/orders` | список / новый заказ (параметры — см. [studio.md](studio.md)) |
| GET | `/api/orders/<id>` | заказ + `estimate_usd` + последняя задача |
| PUT | `/api/orders/<id>` | параметры; `script_text` + `characters` + `script_title` — правка сценария (409, пока идёт задача) |
| POST | `/api/orders/<id>/files` | `{name, data: base64, kind: asset\|logo}` |
| POST | `/api/orders/<id>/files/remove` | `{name}` |
| GET | `/api/orders/<id>/file/<name>` | файл клиента (только из списка заказа) |
| GET | `/api/orders/<id>/video[?dl=1]` | ролик; `dl=1` — скачать с именем по названию |
| POST | `/api/orders/<id>/delete` | удалить заказ с файлами |

## Безопасность

Панель запускает платные генерации и реальную публикацию, поэтому:
- сервер слушает `127.0.0.1` (`API_HOST`), проверяет заголовок `Host` по белому списку (защита от
  DNS rebinding);
- все не-GET запросы — только `Content-Type: application/json` и только same-origin (защита от CSRF);
- для доступа из сети — `API_HOST=0.0.0.0` и обязательный `ADMIN_TOKEN` (сервер без него не стартует),
  плюс `API_ALLOWED_HOSTS`; панель открывается как `/?token=…`, токен хранится в `localStorage`
  и передаётся заголовком `X-Admin-Token` (для видео и картинок — `?token=`);
- выпуск серии требует явного `confirm: true`;
- пути к файлам проверяются: ролики — только внутри `output/`, файлы заказа — только из его списка,
  id сериалов/заказов/запусков — по регулярным выражениям;
- логи и ошибки отдаются через `redact()` (секреты маскируются).

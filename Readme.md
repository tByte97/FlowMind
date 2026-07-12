# FlowMind

FlowMind - система адаптивного керування світлофорами для міської зони на базі SUMO, TraCI, FastAPI та ML-прогнозу черг.

## Трек проєкту

**Smart City + AI**

Проєкт належить до Smart City, тому що вирішує задачу міської транспортної інфраструктури: неефективну роботу світлофорів, поширення заторів між пов'язаними перехрестями та відсутність координованого пріоритету для екстреного транспорту.

AI використовується як технологічна складова для аналізу транспортної ситуації, прогнозування черг і вибору світлофорних фаз. Додатково проєкт можна віднести до напряму **AI / Intelligent Transport Systems**.

## Що робить система

- запускає SUMO-симуляцію дорожньої мережі Рівного;
- читає трафік через TraCI з контрольованих перехресть;
- збирає дані тільки в межах sensor range біля перехресть, за замовчуванням 120 м;
- порівнює режими `static_fixed`, `sumo_actuated`, `local`, `flowmind`;
- керує фазами світлофорів через Python-контролер;
- прогнозує майбутню чергу на 30, 60 і 90 секунд;
- враховує зайнятість вихідних смуг, щоб не випускати авто в заблоковану ділянку;
- підтримує сценарій швидкої допомоги та green corridor;
- записує `live_status.json`, `summary.csv`, ML samples і trace-файли;
- має FastAPI web dashboard для запуску симуляцій, перегляду live-метрик, архіву, середніх значень і Gemini-звіту після ручного натискання кнопки.

## Режими керування

| Режим | Опис |
| --- | --- |
| `static_fixed` | детермінований baseline: активна SUMO-програма копіюється як `STATIC`, а тривалості фаз фіксуються в межах `minDur/maxDur` |
| `sumo_actuated` | штатна actuated-логіка SUMO без зовнішнього FlowMind-контролера |
| `local` | адаптивне керування окремим перехрестям за локальною чергою |
| `flowmind` | зональне керування з урахуванням черги, downstream occupancy, area pressure, demand timer, ML forecast і priority override |

Старе імʼя `fixed` приймається CLI лише як сумісний alias для
`static_fixed`; нові результати завжди записуються з канонічною назвою.
На старті кожного режиму FlowMind перевіряє фактично активні SUMO
`program_id`/`program_type` для всіх TLS і записує їх у
`<mode>_tls_programs_startup.json`, summary та live telemetry.

## Архітектура

```text
Data source (SUMO now; controller/backend API in production)
        ↓
replaceable adapter
        ↓
neutral TrafficState + TLS safety catalog
        ↓
directed AreaGraph + one AreaDecisionSnapshot
        ↓
AreaSignalController
        ↓
signal_policy + queue_forecast + safety_validator
        ↓
traffic light phase updates
        ↓
MetricsCollector
        ↓
results/* + FastAPI dashboard
```

## Основні модулі

| Файл | Призначення |
| --- | --- |
| `flowmind/area_model.py` | модель зони, TLS program ID/type, фазових duration/minDur/maxDur і lane links |
| `flowmind/tls_programs.py` | static fixed-time програма й startup-аудит активних SUMO TLS-програм |
| `flowmind/tls_safety.py` | незалежна від джерела модель рухів, конфліктів і startup-validation всіх TLS plans |
| `flowmind/sumo_tls_adapter.py` | ізольований адаптер SUMO topology/TraCI до нейтрального safety-каталогу |
| `flowmind/zone_graph.py` | corridors, directed road segments, node storage, spillback probability і area decision snapshot |
| `flowmind/sumo_zone_graph_adapter.py` | pre-MVP адаптер, який зв’язує TLS через реальні SUMO edges/lanes |
| `flowmind/traffic_state.py` | читання стану смуг у sensor range |
| `flowmind/signal_policy.py` | scoring фаз для `local` і `flowmind` |
| `flowmind/controller.py` | прийняття рішень і керування світлофорами через TraCI |
| `flowmind/safety_validator.py` | hard floor SUMO minDur, реальні yellow/all-red duration/minDur і безпечні переходи |
| `flowmind/queue_forecast.py` | LightGBM-прогноз черг на 30/60/90 секунд |
| `flowmind/emergency_router.py` | маршрути для швидкої та оцінка альтернатив |
| `flowmind/corridor_manager.py` | стан green corridor для екстреного транспорту |
| `flowmind/sumo_corridor_adapter.py` | SUMO-specific підтвердження фактичного проїзду stop line |
| `flowmind/corridor_recovery.py` | нейтральний recovery planner базової фази та cycle offset |
| `flowmind/zone_boundary.py` | фіксована межа evaluation-зони та directed inflow/outflow edges |
| `flowmind/metrics.py` | zone KPI, completed/censored trips, emergency trace і summary |
| `flowmind/provenance.py` | hashes сценарію, demand, мережі, конфігурації, controller і моделей |
| `flowmind/evaluation.py` | paired CI/tests та regression gates без підміни `null` нулем |
| `experiments/run_demo.py` | demo-запуск FlowMind з опціональним static fixed baseline |
| `experiments/run_evaluation.py` | resumable benchmark 30–50 повних пар усіх режимів |
| `experiments/run_dataset.py` | генерація ML dataset через багато симуляцій |
| `experiments/train_queue_ensemble.py` | тренування моделей 30/60/90 секунд |
| `api/web_dashboard.py` | FastAPI dashboard, API, archive, averages, Gemini summary |

## AI / ML

Поточні runtime-моделі:

```text
models/queue_lgbm_30s_current.joblib
models/queue_lgbm_60s_current.joblib
models/queue_lgbm_90s_current.joblib
```

Модельний стек:

- LightGBM;
- 3 горизонти прогнозу: 30, 60, 90 секунд;
- target: `target_incoming_queue_30s`, `target_incoming_queue_60s`, `target_incoming_queue_90s`;
- контракт моделей: `current_policy`; фактичний `current_phase` не підміняється candidate-фазою;
- input features: стан фази, elapsed time, incoming/outgoing queue, occupancy, speed, free slots, signal state, TLS/lane/movement ID як categorical features і control parameters;
- кожен артефакт має SHA-256 dataset, network, feature schema та `.joblib` у sidecar metadata;
- prediction обмежується фізичною lane capacity;
- unseen TLS/lane, schema/hash mismatch або feature OOD автоматично вимикають ML-вплив.

За замовчуванням ML працює у shadow mode: прогноз не впливає на
світлофор, а trace після закінчення горизонту містить фактичну чергу і MAE.
Явно дозволити лише in-domain/high-confidence ML-вплив можна прапором
`--enable-queue-control`.

## Green corridor

Система підтримує demo-сценарій швидкої:

- створення emergency vehicle у SUMO;
- вибір маршруту до лікарні;
- визначення світлофорів на маршруті;
- `PREPARE` м’яко готує до трьох наступних TLS, а `GREEN_WINDOW` дає hard priority лише активному TLS;
- проїзд TLS зараховується лише після SUMO-adapter stop-line confirmation;
- hard downstream storage/spillback gate може відхилити навіть emergency priority;
- перед departure маршрут повторно оцінюється за актуальними чергами;
- після corridor recovery planner повертає базову фазу і cycle offset без пропуску clearance;
- emergency-route TLS, який не пройшов startup safety validation, залишається під штатною програмою і не отримує FlowMind-команд;
- метрики `emergency_departure_time`, `emergency_arrival_time`, `emergency_eta`, `priority_decisions`;
- `*_civilian_priority_impact.csv` і summary delta вимірюють фактичний вплив на цивільні авто.

## Dashboard

FastAPI dashboard:

- `/` - live dashboard;
- `/archive` - архів запусків;
- `/averages` - порівняння `static_fixed` / `sumo_actuated` / `local` / `flowmind`;
- `/api/status` - поточний live status;
- `/api/start-demo` - запуск demo-симуляції;
- `/api/stop` - зупинка процесу;
- `/api/archive` - список результатів;
- `/api/averages` - агреговані середні;
- `/api/gemini-summary` - ручна генерація текстового висновку після симуляції.

Gemini не запускається автоматично. Висновок формується тільки після натискання кнопки в UI. Якщо `GEMINI_API_KEY` не заданий або API недоступний, dashboard показує локальний fallback-звіт.

## Метрики

Основні KPI:

| Метрика | Джерело |
| --- | --- |
| `average_travel_time` | `summary.csv` |
| `completed_trips` / `unfinished_trips` | кількість завершених і censored поїздок |
| `unfinished_travel_time_lower_bound_mean` | нижня межа часу незавершених поїздок |
| `average_waiting_time` | `summary.csv` |
| `average_queue_length` | `summary.csv` |
| `max_queue_length` | `summary.csv` |
| `zone_inflow` / `zone_outflow` | перетини directed boundary edges evaluation-зони |
| `throughput` | alias фактичного `zone_outflow` для сумісності dashboard |
| `departed_vehicles` | `summary.csv` |
| `peak_active_vehicles` | `summary.csv` |
| `stops_count` | `summary.csv` |
| `blocked_outgoing_share` | фактична частка заблокованих outgoing lanes |
| `queue_forecast_predictions` | controller stats |
| `priority_decisions` | emergency priority stats |

Якщо метрику неможливо обчислити (немає валідних lane samples, завершеної
поїздки або emergency arrival), summary містить JSON `null`, а не штучний `0`.
Evaluation-зона завжди залишається початковою зоною з конфігурації; тимчасове
розширення control-зони вздовж emergency corridor не змінює межі вимірювання.

## Технології

| Рівень | Технології |
| --- | --- |
| Traffic simulation | Eclipse SUMO, sumolib, TraCI |
| Backend / control | Python 3.12, FastAPI, Uvicorn |
| ML | LightGBM, scikit-learn, pandas, joblib |
| Graph / routing | networkx |
| Data | CSV, JSON, SUMO XML |
| Dashboard | FastAPI HTML/CSS/JS без frontend framework |
| Deployment | Docker, Docker Compose, nginx reverse proxy |
| Optional report | Google Gemini API через `google-genai` |

## Запуск локально

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.runtime.txt
python -m uvicorn api.web_dashboard:app --host 127.0.0.1 --port 8011
```

Dashboard:

```text
http://127.0.0.1:8011/
```

## Запуск demo без UI

```bash
python experiments/run_demo.py --duration 600 --seed 42 --headless --no-dashboard
```

З static fixed baseline + FlowMind:

```bash
python experiments/run_demo.py --duration 600 --seed 42 --headless --no-dashboard
```

Тільки FlowMind без baseline:

```bash
python experiments/run_demo.py --duration 600 --seed 42 --headless --no-dashboard --no-baseline
```

Окремі режими без demo-обгортки:

```bash
python experiments/run_experiment.py static_fixed --duration 600
python experiments/run_experiment.py sumo_actuated --duration 600
python experiments/run_experiment.py local --duration 600
python experiments/run_experiment.py flowmind --duration 600
python experiments/run_experiment.py flowmind --duration 600 --emergency
# ML вплив лише після shadow-валідації:
python experiments/run_experiment.py flowmind --duration 600 --enable-queue-control
```

## Відтворювана оцінка режимів

Повний benchmark запускає 30 пар (120 SUMO runs) для
`static_fixed`, `sumo_actuated`, `local` і `flowmind`:

```bash
python experiments/run_evaluation.py \
  --replicates 30 \
  --duration 1800 \
  --workers 4 \
  --evaluation-id rivne_full_v1
```

У межах кожної пари однакові seed, scenario/network/demand hashes, control
config і фактичний маршрут швидкої. Після першого режиму маршрут фіксується
як список SUMO edges для решти трьох режимів; pre-departure rerouting у
benchmark вимкнено. Перерваний запуск можна продовжити з тими самими
параметрами та `--resume`.

Runner формує `evaluation_manifest.json`, `summaries.json`,
`evaluation_report.json`, `paired_metrics.csv`, `regression_gates.csv` і
`validated_pairs.csv`. Звіт містить 95% CI paired differences, двосторонній
paired t-test і non-regression gates для waiting, zone outflow, stops,
blocked outgoing share та emergency ETA. Повна методика і точні допуски:
[`docs/EVALUATION.md`](docs/EVALUATION.md).

## Docker

```bash
docker compose -f compose.yaml -f compose.server.yaml up -d --build web
```

Основні env-змінні:

```text
FLOWMIND_RESULTS_DIR=/app/results
FLOWMIND_WEB_RESULTS_DIR=/app/results/web_demo
FLOWMIND_CPU_THREADS=4
FLOWMIND_EVALUATION_REPLICATES=30
FLOWMIND_EVALUATION_DURATION=1800
FLOWMIND_EVALUATION_WORKERS=4
FLOWMIND_EVALUATION_ID=rivne_full_v1
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.1-flash-lite
```

`/app/results` винесено в persistent volume `flowmind_results`, а активні
моделі — у `flowmind_models`. Перший запуск копіює bundled models з image у
порожній models volume; trainer потім оновлює цей volume.

Серверний workflow:

```bash
# 1. Зібрати образ і підняти web.
docker compose -f compose.yaml -f compose.server.yaml build web
docker compose -f compose.yaml -f compose.server.yaml up -d web

# 2. Зібрати повний training dataset (400 resumable runs).
docker compose --profile dataset \
  -f compose.yaml -f compose.server.yaml up dataset

# 3. Навчити ensemble; під час цього не запускати нові симуляції.
docker compose --profile training \
  -f compose.yaml -f compose.server.yaml up trainer

# 4. Виконати resumable 30-pair evaluation на нових моделях.
docker compose --profile evaluation \
  -f compose.yaml -f compose.server.yaml up evaluation
```

Jobs можна від'єднати від terminal через `up -d`, а стан дивитися командами
`docker compose logs -f dataset`, `docker compose logs -f trainer` і
`docker compose logs -f evaluation`. Повторний `up` використовує `--resume`
для dataset/evaluation і не приймає неповні summaries як завершені runs.

## Dataset і тренування

Генерація dataset:

```bash
python experiments/run_dataset.py --full-real --resume --keep-going --output-dir results/dataset
```

Тренування ensemble:

```bash
python experiments/train_queue_ensemble.py \
  --dataset-dir results/dataset \
  --output-dir models \
  --rows-per-file 5000 \
  --jobs 4
```

## Структура

```text
FlowMind/
├── api/                         # FastAPI dashboard/API
├── flowmind/                    # control logic, metrics, ML forecast, emergency corridor
├── experiments/                 # demo, dataset, training, comparison scripts
├── models/                      # current LightGBM queue models
├── simulation/rivne_area/       # SUMO map/config/routes
├── tools/                       # SUMO map and focused traffic generation
├── tests/                       # unit tests
├── docs/                        # project documentation
├── Dockerfile
├── compose.yaml
├── compose.server.yaml
└── requirements.runtime.txt
```

## Поточний статус

Реалізовано:

- SUMO-сценарій Рівного;
- static_fixed/sumo_actuated/local/flowmind режими;
- sensor-window traffic reader;
- area pressure controller;
- safety validator;
- ML queue forecast ensemble;
- emergency vehicle + green corridor demo;
- FastAPI dashboard;
- archive and averages pages;
- manual Gemini summary;
- Docker deployment with persistent results volume.

Обмеження:

- це simulation MVP, не production-система для міського контролера;
- для реального впровадження потрібні дані з камер/радарів/індукційних петель;
- ML-моделі бажано перенавчати або fine-tune під конкретне місто і зону;
- pedestrian demand може бути доданий як окремий шар попиту, але зараз не є production-ready частиною логіки.

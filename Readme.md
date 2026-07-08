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
- порівнює режими `fixed`, `local`, `flowmind`;
- керує фазами світлофорів через Python-контролер;
- прогнозує майбутню чергу на 30, 60 і 90 секунд;
- враховує зайнятість вихідних смуг, щоб не випускати авто в заблоковану ділянку;
- підтримує сценарій швидкої допомоги та green corridor;
- записує `live_status.json`, `summary.csv`, ML samples і trace-файли;
- має FastAPI web dashboard для запуску симуляцій, перегляду live-метрик, архіву, середніх значень і Gemini-звіту після ручного натискання кнопки.

## Режими керування

| Режим | Опис |
| --- | --- |
| `fixed` | SUMO/fixed-plan baseline без адаптації до поточного трафіку |
| `local` | адаптивне керування окремим перехрестям за локальною чергою |
| `flowmind` | зональне керування з урахуванням черги, downstream occupancy, area pressure, demand timer, ML forecast і priority override |

## Архітектура

```text
SUMO network / focused.sumocfg
        ↓
SUMO simulation
        ↓
TraCI
        ↓
TrafficStateReader
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
| `flowmind/area_model.py` | модель контрольованої зони, світлофорів, фаз і lane links |
| `flowmind/traffic_state.py` | читання стану смуг у sensor range |
| `flowmind/signal_policy.py` | scoring фаз для `local` і `flowmind` |
| `flowmind/controller.py` | прийняття рішень і керування світлофорами через TraCI |
| `flowmind/safety_validator.py` | перевірка min-green, yellow/all-red і безпечних переходів |
| `flowmind/queue_forecast.py` | LightGBM-прогноз черг на 30/60/90 секунд |
| `flowmind/emergency_router.py` | маршрути для швидкої та оцінка альтернатив |
| `flowmind/corridor_manager.py` | стан green corridor для екстреного транспорту |
| `flowmind/metrics.py` | KPI, history, emergency trace, summary |
| `experiments/run_demo.py` | demo-запуск FlowMind з опціональним fixed baseline |
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
- input features: стан фази, elapsed time, incoming/outgoing queue, occupancy, speed, free slots, signal state, TLS id, movement hashes, control parameters.

Прогноз не замінює контролер. Він додається як один із факторів у scoring фази.

## Green corridor

Система підтримує demo-сценарій швидкої:

- створення emergency vehicle у SUMO;
- вибір маршруту до лікарні;
- визначення світлофорів на маршруті;
- priority override для потрібних фаз;
- метрики `emergency_departure_time`, `emergency_arrival_time`, `emergency_eta`, `priority_decisions`;
- повернення контролера до нормального режиму після проїзду.

## Dashboard

FastAPI dashboard:

- `/` - live dashboard;
- `/archive` - архів запусків;
- `/averages` - порівняння середніх значень `fixed` / `local` / `flowmind`;
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
| `average_waiting_time` | `summary.csv` |
| `average_queue_length` | `summary.csv` |
| `max_queue_length` | `summary.csv` |
| `throughput` | `summary.csv` / `live_status.json` |
| `departed_vehicles` | `summary.csv` |
| `peak_active_vehicles` | `summary.csv` |
| `stops_count` | `summary.csv` |
| `gridlock_risk` | `summary.csv` / live samples |
| `queue_forecast_predictions` | controller stats |
| `priority_decisions` | emergency priority stats |

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

З fixed baseline + FlowMind:

```bash
python experiments/run_demo.py --duration 600 --seed 42 --headless --no-dashboard
```

Тільки FlowMind без baseline:

```bash
python experiments/run_demo.py --duration 600 --seed 42 --headless --no-dashboard --no-baseline
```

## Docker

```bash
docker compose -f compose.yaml -f compose.server.yaml up -d --build web
```

Основні env-змінні:

```text
FLOWMIND_RESULTS_DIR=/app/results
FLOWMIND_WEB_RESULTS_DIR=/app/results/web_demo
FLOWMIND_CPU_THREADS=4
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.1-flash-lite
```

`/app/results` винесено в persistent Docker volume `flowmind_results`.

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
- fixed/local/flowmind режими;
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

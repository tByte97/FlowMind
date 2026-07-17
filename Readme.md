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
| `flowmind/sumo_signal_mask_adapter.py` | fail-closed маскування blocked signal groups без змін phase order, timing або clearance |
| `flowmind/traffic_state.py` | читання стану смуг у sensor range |
| `flowmind/signal_policy.py` | scoring фаз для `local` і `flowmind` |
| `flowmind/controller.py` | прийняття рішень і керування світлофорами через TraCI |
| `flowmind/zone_optimizer.py` | спільна 30–90-секундна ціль і узгодження фаз усієї зони |
| `flowmind/runtime_contract.py` | startup-перевірка commit/TLS/network/zone/schema/model coverage |
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

Активні decision-моделі після нового навчання:

```text
models/queue_lgbm_30s_decision.joblib
models/queue_lgbm_60s_decision.joblib
models/queue_lgbm_90s_decision.joblib
```

Модельний стек:

- LightGBM;
- 3 горизонти прогнозу: 30, 60, 90 секунд;
- target: `target_queue_reduction_30s/60s/90s`;
- контракт моделей: `observational_action_conditioned`; dataset зберігає лише
  фактично виконаний `action_phase`, не вигадуючи outcome альтернативної фази;
- input features включають queue growth 15/30 с, arrival/discharge rate,
  downstream storage/occupancy, platoon, сусідні TLS та demand profile;
- кожен артефакт має SHA-256 dataset, dataset fingerprint,
  network, zone, dataset/feature schema та `.joblib`;
- prediction обмежується фізичною lane capacity;
- unseen TLS/lane/action/category, schema/hash mismatch або feature OOD
  автоматично вимикають ML-вплив.

За замовчуванням ML працює у shadow mode: прогноз не впливає на
світлофор, а trace порівнює прогноз зменшення черги лише для
фактично виконаної фази. Альтернативні candidate-фази не видаються
за спостережений outcome.
Явно дозволити лише in-domain/high-confidence ML-вплив можна прапором
`--enable-queue-control`, і лише з approval-артефактом, що збігається з
model/network/zone/ControlConfig hashes.

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
# ML вплив лише після shadow/control evaluation і approval:
python experiments/run_demo.py --duration 600 --headless --no-dashboard \
  --enable-queue-control --control-config results/tuning/best_control_config.json
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
FLOWMIND_REQUIRE_MUTATION_AUTH=1
FLOWMIND_MUTATION_TOKEN=<long-random-secret>
```

`/app/results` винесено в persistent volume `flowmind_results`, а активні
моделі — у `flowmind_models`. Volume моделей автоматично
ініціалізується з UID/GID `10001`, тому trainer може безпечно записувати
артефакти. Старі 6-TLS моделі лежать у `models/archive/zone6` і не
потрапляють у Docker image.

Серверний workflow:

```bash
# 1. Зібрати versioned image і підняти web.
./scripts/build_image.sh web
docker compose -f compose.yaml -f compose.server.yaml up -d web

# 2. Зібрати повний training dataset (400 resumable runs).
docker compose --profile dataset \
  -f compose.yaml -f compose.server.yaml up dataset

# 3. Audit автоматично перевіряє рівно 400 runs, після нього trainer
#    навчається лише на accepted samples.
docker compose --profile training \
  -f compose.yaml -f compose.server.yaml up trainer

# 4. Не вмикати ML: спочатку shadow/control evaluation та approval,
#    точні команди наведені нижче.

# Окремий нічний snapshot-branch pipeline: збір реальних outcomes
# альтернативних фаз, quality gate і навчання candidate ensemble.
docker compose --profile counterfactual \
  -f compose.yaml -f compose.server.yaml up -d counterfactual-trainer
```

Jobs можна від'єднати від terminal через `up -d`, а стан дивитися командами
`docker compose logs -f dataset`, `docker compose logs -f trainer` і
`docker compose logs -f evaluation`. Повторний `up` використовує `--resume`
для dataset/evaluation і не приймає неповні summaries як завершені runs.
Dataset `--resume` додатково fail-fast порівнює fingerprint плану,
мережі, зони, schema, controller source, demand і всіх run settings.
Web-сторінка `/jobs` показує dataset progress/ETA/disk, quality gate,
trainer/evaluation та model registry. Mutation endpoints (`start`, `stop`,
Gemini) в server Compose вимагають `FLOWMIND_MUTATION_TOKEN`.

## Dataset і тренування

Генерація dataset:

```bash
python experiments/run_dataset.py --full-real --resume --keep-going --output-dir results/dataset
```

Обов'язковий quality audit перед ручним тренуванням:

```bash
python experiments/audit_dataset.py --dataset-dir results/dataset \
  --expected-runs 400 --output results/dataset/quality_report.json
```

Аудит рахує teleports (jam/yield/wrong-lane), emergency braking, collisions,
actuated detector coverage та формує accepted/rejected manifest. Schema v3
поточного серверного dataset мігрує в пам'яті до observational contract;
повторювати 400 runs не потрібно.

Тренування ensemble:

```bash
python experiments/train_queue_ensemble.py \
  --dataset-dir results/dataset \
  --output-dir models \
  --rows-per-file 5000 \
  --jobs 4 \
  --quality-report results/dataset/quality_report.json
```

Звичайний dataset має observational contract і не містить результатів
альтернативних фаз. Для decision-моделі використовується окремий schema v5
snapshot-branch dataset: SUMO зберігає стан і RNG, кожна green action
відтворюється через повний reload (щоб не втратити майбутні `<flow>` vehicles),
а перемикання проходить через yellow/all-red clearance. Нічний Compose job
пише dataset у `results/counterfactual_dataset`, а моделі — лише в
`models/counterfactual_candidate`; production-моделі він автоматично не
замінює. Після завершення потрібні shadow paired evaluation та approval gate.

Перед tuning/training можна запустити п'ять коротких ablation-профілів:

```bash
python experiments/run_ablation.py --replicates 10 --duration 900 \
  --workers 2 --resume
```

Final evaluation суворо відхиляє `sumo_actuated`, якщо SUMO startup log
містить хоча б один link без detector coverage. Прапорець
`--allow-incomplete-actuated-detectors` дозволений лише для smoke/archive.

Валідація, tuning та допуск ML:

```bash
# 1. Bayesian/Optuna search зональної ControlConfig.
docker compose --profile tuning up tuning

# 2. 30 paired replicates: ML лише shadow.
python experiments/run_evaluation.py --replicates 30 --duration 1800 \
  --workers 4 --evaluation-id rivne_shadow_v2 --resume \
  --control-config results/tuning/best_control_config.json

# 3. 30 paired replicates: ML вплив у кандидатному evaluation.
python experiments/run_evaluation.py --replicates 30 --duration 1800 \
  --workers 4 --evaluation-id rivne_ml_control_v2 --resume \
  --enable-queue-control \
  --control-config results/tuning/best_control_config.json

# 4. Видати hash-bound approval лише якщо обидва reports pass.
python experiments/approve_queue_control.py \
  --shadow-dir results/evaluation/rivne_shadow_v2 \
  --baseline-dir results/evaluation/rivne_shadow_v2 \
  --control-dir results/evaluation/rivne_ml_control_v2
```

Для web після approval встановити
`FLOWMIND_ENABLE_QUEUE_CONTROL=1` і
`FLOWMIND_CONTROL_CONFIG=/app/results/tuning/best_control_config.json`.

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

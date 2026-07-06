# ML training guide для FlowMind

Цей документ описує правильний цикл роботи з ML у FlowMind:

```text
SUMO simulations
  -> dataset CSV
  -> train queue forecast model
  -> evaluate on unseen seeds
  -> version model
  -> connect forecast to FlowMind controller
  -> SafetyValidator remains the final gate
```

Головна задача першої ML-моделі — не напряму перемикати світлофори, а
прогнозувати майбутню чергу або завантаженість. Рішення все одно має
проходити через FlowMind controller і `SafetyValidator`.

```text
Traffic state
  -> ML forecast
  -> FlowMind scoring
  -> SafetyValidator
  -> TraCI
```

## 1. Що саме тренуємо

Перший практичний target:

```text
target_incoming_queue_60s
```

Це прогноз черги на вхідній смузі через 60 секунд. Він достатньо далекий,
щоб бути корисним для планування, але ще не такий нестабільний, як 90
секунд.

Додаткові target-и:

```text
target_incoming_queue_30s
target_incoming_queue_90s
target_incoming_occupancy_30s
target_incoming_occupancy_60s
target_incoming_occupancy_90s
target_outgoing_occupancy_30s
target_outgoing_occupancy_60s
target_outgoing_occupancy_90s
target_downstream_blocked_30s
target_downstream_blocked_60s
target_downstream_blocked_90s
```

Для MVP достатньо почати з `target_incoming_queue_60s`.

## 2. Де лежать дані й моделі

Dataset:

```text
results/dataset/
├── dataset_index.csv
├── samples/
│   ├── rivne_focused_fixed_seed_00042.csv
│   ├── rivne_focused_local_seed_00042.csv
│   └── rivne_focused_flowmind_seed_00042.csv
└── summaries/
```

Моделі:

```text
models/
├── queue_lgbm_60s.joblib
└── queue_lgbm_60s_metadata.json
```

`*.joblib` містить:

- preprocessing pipeline;
- one-hot encoding категоріальних ознак;
- навчену LightGBM або XGBoost модель;
- metadata всередині artifact.

`*_metadata.json` містить:

- target;
- список features;
- train/validation/test seeds;
- MAE, RMSE, R2;
- top feature importances;
- дату створення моделі.

Прихованого окремого кешу моделі немає. Можуть з’являтися тільки
звичайні Python-файли:

```text
__pycache__/
*.pyc
```

Вони не є навченими даними і не потрібні для роботи моделі.

## 3. Встановити ML-залежності

Активувати середовище:

```bash
source .venv/bin/activate
```

Встановити залежності:

```bash
pip install -r requirements.txt
```

Або мінімально для тренування:

```bash
pip install scikit-learn lightgbm joblib
```

Перевірити:

```bash
python -c "import lightgbm, sklearn, joblib; print('ML deps OK')"
```

## 4. Створення dataset

Повний рекомендований dataset:

```bash
python experiments/run_dataset.py --runs-per-mode 50
```

Це запустить:

```text
50 local
50 fixed
50 flowmind
= 150 simulations
```

За замовчуванням використовується:

```text
simulation/new_area/focused.sumocfg
simulation/new_area/central_zone.json
duration = 1800 секунд
sample_interval = 5 секунд
target_horizons = 30, 60, 90 секунд
```

Продовжити після переривання:

```bash
python experiments/run_dataset.py --runs-per-mode 50 --resume
```

Короткий тест:

```bash
python experiments/run_dataset.py --runs-per-mode 2
```

Інший інтервал семплування:

```bash
python experiments/run_dataset.py --runs-per-mode 50 --sample-interval 3
```

Інша тривалість:

```bash
python experiments/run_dataset.py --runs-per-mode 50 --duration 900
```

Більш різноманітний dataset для майбутньої ML-моделі:

```bash
python experiments/run_dataset.py \
  --runs-per-mode 50 \
  --randomize \
  --duration-min 600 \
  --duration-max 1800 \
  --plan-seed 20260705
```

У цьому режимі зона лишається та сама з `--zone`, але кожна симуляція
отримує:

- випадковий SUMO seed;
- випадкову тривалість у межах `--duration-min/--duration-max`;
- випадковий, але сумісний з target horizons `sample_interval`;
- випадкові параметри `ControlConfig`: `min_green`, `max_green`,
  `decision_interval`, `blocked_occupancy`, `downstream_weight`,
  `area_pressure_weight`, `queue_forecast_weight`, `hysteresis` та інші.

Перед довгим запуском перевірити план без SUMO:

```bash
python experiments/run_dataset.py --runs-per-mode 2 --randomize --dry-run
```

`--plan-seed` робить сам план відтворюваним. Якщо треба створити новий
набір різних симуляцій, змінити `--plan-seed` або `--output-dir`.

## 5. Як перевірити dataset перед тренуванням

Кількість CSV:

```bash
find results/dataset/samples -maxdepth 1 -name '*.csv' | wc -l
```

Переглянути перший файл:

```bash
head -3 results/dataset/samples/rivne_focused_local_seed_00042.csv
```

Перевірити кількість рядків:

```bash
wc -l results/dataset/samples/rivne_focused_local_seed_00042.csv
```

Переглянути індекс:

```bash
head results/dataset/dataset_index.csv
```

У `dataset_index.csv` важливо дивитися:

- `status`;
- `dataset_rows`;
- `throughput`;
- `departed_vehicles`;
- `average_waiting_time`;
- `controller_decisions`.

Якщо `dataset_rows = 0`, такий запуск не годиться для ML.

## 6. Features у dataset

Основні колонки з dataset:

```text
mode
time
tls_id
movement_id
incoming_lane
outgoing_lane
signal_index
signal_state
is_green
current_phase
phase_state
phase_elapsed
phase_count
incoming_queue
incoming_vehicle_count
incoming_occupancy
incoming_mean_speed
incoming_free_slots
outgoing_queue
outgoing_vehicle_count
outgoing_occupancy
outgoing_mean_speed
outgoing_free_slots
downstream_blocked
movement_hash
incoming_lane_hash
outgoing_lane_hash
```

`movement_id`, `incoming_lane` і `outgoing_lane` читаються з CSV, але не
подаються в модель як великі one-hot категорії. Trainer перетворює їх у:

```text
movement_hash
incoming_lane_hash
outgoing_lane_hash
```

Це сильно зменшує RAM usage.

Фактичні model features:

```text
mode
time
tls_id
signal_index
signal_state
is_green
current_phase
phase_state
phase_elapsed
phase_count
incoming_queue
incoming_vehicle_count
incoming_occupancy
incoming_mean_speed
incoming_free_slots
outgoing_queue
outgoing_vehicle_count
outgoing_occupancy
outgoing_mean_speed
outgoing_free_slots
downstream_blocked
movement_hash
incoming_lane_hash
outgoing_lane_hash
```

Не використовувати як features:

```text
target_*
run_id
scenario
duration
```

`seed` використовується тільки для правильного split. Не варто подавати
`seed` як ознаку в модель.

## 7. Dry-run тренування

Перед реальним тренуванням перевірити, що дані читаються і split
працює:

```bash
python experiments/train_queue_model.py --dry-run
```

Для швидкої перевірки на частині даних:

```bash
python experiments/train_queue_model.py \
  --dry-run \
  --max-files 9 \
  --rows-per-file 500
```

Очікуваний вивід:

```text
Samples: ... files
Rows after target cleanup: ...
Target: target_incoming_queue_60s
Modes: fixed, local, flowmind
Split rows: train=..., validation=..., test=...
Split seeds: train=..., validation=..., test=...
```

Якщо dry-run не проходить, реальне тренування запускати не треба.

## 8. Навчити модель

Базове тренування:

```bash
python experiments/train_queue_model.py
```

За замовчуванням:

```text
model_type = lightgbm
target = target_incoming_queue_60s
output = models/queue_lgbm_60s.joblib
metadata = models/queue_lgbm_60s_metadata.json
rows_per_file = 2000
```

За замовчуванням trainer не вантажить весь dataset у RAM. Він бере до
`2000` рядків з кожного CSV, читає тільки потрібні колонки й перетворює
важкі ID-колонки у стабільні числові hash-features.

Для машини з 16 GB RAM рекомендовано почати так:

```bash
python experiments/train_queue_model.py \
  --rows-per-file 1000 \
  --output models/queue_lgbm_60s_v1.joblib
```

Якщо пройшло без `Вбито`, можна збільшити:

```bash
python experiments/train_queue_model.py \
  --rows-per-file 2000 \
  --output models/queue_lgbm_60s_v2.joblib
```

Повний dataset вмикати тільки якщо вистачає RAM:

```bash
python experiments/train_queue_model.py \
  --full-dataset \
  --output models/queue_lgbm_60s_full_v1.joblib
```

Навчити 30/60/90 секунд:

```bash
python experiments/train_queue_model.py \
  --target target_incoming_queue_30s \
  --output models/queue_lgbm_30s_v1.joblib

python experiments/train_queue_model.py \
  --target target_incoming_queue_60s \
  --output models/queue_lgbm_60s_v1.joblib

python experiments/train_queue_model.py \
  --target target_incoming_queue_90s \
  --output models/queue_lgbm_90s_v1.joblib
```

Тренування тільки на FlowMind-режимі:

```bash
python experiments/train_queue_model.py \
  --modes flowmind \
  --target target_incoming_queue_60s \
  --output models/queue_lgbm_60s_flowmind_only_v1.joblib
```

Для першої production-like моделі краще використовувати всі режими:

```text
fixed + local + flowmind
```

Так модель бачить і погані затори, і адаптивну поведінку системи.

## 9. Як зрозуміти, що тренування йде

Під час старту скрипт має вивести:

```text
Samples: ... files
Rows after target cleanup: ...
Split rows: ...
Training model...
```

Після рядка:

```text
Training model...
```

може бути пауза. Це нормально: LightGBM тренується всередині процесу.

Перевірити процес:

```bash
ps -eo pid,etime,pcpu,pmem,cmd | grep train_queue_model
```

Дивитися CPU/RAM:

```bash
top
```

Або:

```bash
htop
```

Перевірити, чи створюються artifacts:

```bash
ls -lh models
```

Поки тренування не завершилось, `.joblib` може ще не існувати. Це
нормально: файл пишеться після `pipeline.fit()`.

Якщо процес довго працює і CPU високий — тренування йде. Якщо CPU майже
нульовий і немає нових повідомлень дуже довго, перевірити пам’ять:

```bash
free -h
```

Для дуже великого dataset можна тимчасово обмежити rows:

```bash
python experiments/train_queue_model.py --rows-per-file 1000
```

Якщо система пише:

```text
Вбито
```

це майже завжди означає, що Linux OOM-killer зупинив процес через брак
RAM. У такому випадку зменшити sample:

```bash
python experiments/train_queue_model.py --rows-per-file 500
```

або тимчасово тренуватися на частині файлів:

```bash
python experiments/train_queue_model.py --max-files 30 --rows-per-file 1000
```

## 10. Як читати результат

Після завершення буде:

```text
Model saved:    models/queue_lgbm_60s.joblib
Metadata saved: models/queue_lgbm_60s_metadata.json
{
  "train": {...},
  "validation": {...},
  "test": {...}
}
```

Головні метрики:

```text
MAE  — середня абсолютна помилка в автомобілях черги
RMSE — сильніше карає великі помилки
R2   — наскільки модель краща за простий baseline
```

Приклад інтерпретації:

```text
test MAE = 1.2
```

означає, що в середньому модель помиляється приблизно на 1.2 автомобіля
для прогнозу черги.

Для першої моделі добре, якщо:

```text
test MAE низький
test RMSE не сильно більший за MAE
test R2 > 0
validation і test близькі
```

Поганий сигнал:

```text
train MAE дуже низький
validation/test MAE сильно гірший
```

Це означає overfitting.

## 11. Як уникнути регресу

Не перезаписувати хорошу модель без перевірки. Використовувати версії:

```text
models/
├── queue_lgbm_60s_v1.joblib
├── queue_lgbm_60s_v1_metadata.json
├── queue_lgbm_60s_v2.joblib
├── queue_lgbm_60s_v2_metadata.json
└── queue_lgbm_60s_current.joblib
```

Тренувати нову версію:

```bash
python experiments/train_queue_model.py \
  --target target_incoming_queue_60s \
  --output models/queue_lgbm_60s_v2.joblib
```

Порівняти metadata:

```bash
cat models/queue_lgbm_60s_v1_metadata.json
cat models/queue_lgbm_60s_v2_metadata.json
```

Нова модель приймається тільки якщо на `test`:

```text
MAE не збільшився
RMSE не збільшився
R2 не впав
```

Правило:

```text
new_test_mae  <= old_test_mae
new_test_rmse <= old_test_rmse
new_test_r2   >= old_test_r2
```

Якщо модель краща, можна зробити її поточною:

```bash
cp models/queue_lgbm_60s_v2.joblib models/queue_lgbm_60s_current.joblib
cp models/queue_lgbm_60s_v2_metadata.json models/queue_lgbm_60s_current_metadata.json
```

## 12. Правильний split

Не можна робити random split по рядках, бо сусідні моменти часу дуже
схожі. Це створить витік даних.

Правильно:

```text
split by seed
```

`train_queue_model.py` уже ділить дані по seed.

Приклад:

```text
train: seed 42..111
validation: seed 112..126
test: seed 127..141
```

Test seed-и не повинні використовуватися для тренування.

## 13. Як оновлювати dataset

Якщо просто треба дозібрати пропущені симуляції:

```bash
python experiments/run_dataset.py --runs-per-mode 50 --resume
```

Якщо змінена логіка контролера, карта, трафік або фічі — краще створити
новий dataset name:

```bash
python experiments/run_dataset.py \
  --runs-per-mode 50 \
  --scenario-name rivne_focused_v2 \
  --output-dir results/dataset_v2
```

Потім тренувати:

```bash
python experiments/train_queue_model.py \
  --dataset-dir results/dataset_v2 \
  --output models/queue_lgbm_60s_v2.joblib
```

Не змішувати старий і новий dataset без позначення версії, якщо:

- змінилася карта;
- змінився список контрольованих світлофорів;
- змінилася генерація traffic demand;
- змінилася структура features;
- змінився алгоритм controller так, що поведінка стала іншою.

## 14. Коли dataset треба перегенерувати

Перегенерувати dataset потрібно, якщо змінились:

- `simulation/new_area/focused.sumocfg`;
- `simulation/new_area/focused.rou.xml`;
- `simulation/new_area/central_zone.json`;
- `flowmind/signal_policy.py`;
- `flowmind/controller.py`;
- `flowmind/safety_validator.py`;
- `flowmind/ml_dataset.py`;
- sampling interval;
- target horizons.

Якщо змінилася тільки документація або dashboard, dataset можна не
перегенеровувати.

## 15. Підключення моделі до системи

Поточний production-forecast використовує ensemble з трьох горизонтів:

```text
models/queue_lgbm_30s_current.joblib
models/queue_lgbm_60s_current.joblib
models/queue_lgbm_90s_current.joblib
```

Зараз це aliases на перевірені моделі:

```text
models/queue_lgbm_30s_v1.joblib
models/queue_lgbm_60s_v5_20k.joblib
models/queue_lgbm_90s_v2_20k.joblib
```

Default blend у `ControlConfig`:

```text
30s = 0.50
60s = 0.35
90s = 0.15
```

Runtime-шар:

```text
flowmind/queue_forecast.py
```

Його відповідальність:

1. завантажити current-моделі 30/60/90s;
2. прочитати `feature_columns` з metadata artifact;
3. зібрати поточні features у тому ж форматі, що й dataset;
4. передбачити майбутню чергу на кожному горизонті;
5. змішати прогнози у `QueueForecastEnsemble`;
6. повернути прогноз у `signal_policy.py`.

Правильна інтеграція:

```text
TrafficStateReader
  -> QueueForecastModel / QueueForecastEnsemble
  -> signal_policy score
  -> AreaSignalController
  -> SafetyValidator
  -> TraCI
```

Запуск з одним конкретним горизонтом:

```bash
python experiments/run_experiment.py flowmind \
  --config simulation/new_area/focused.sumocfg \
  --zone simulation/new_area/central_zone.json \
  --duration 900 \
  --queue-model models/queue_lgbm_30s_current.joblib
```

Запуск з кастомним набором горизонтів:

```bash
python experiments/run_experiment.py flowmind \
  --config simulation/new_area/focused.sumocfg \
  --zone simulation/new_area/central_zone.json \
  --duration 900 \
  --queue-model \
    models/queue_lgbm_30s_current.joblib \
    models/queue_lgbm_90s_current.joblib
```

Запуск FlowMind з ML-прогнозом:

```bash
python experiments/run_experiment.py flowmind \
  --config simulation/new_area/focused.sumocfg \
  --zone simulation/new_area/central_zone.json \
  --duration 900
```

Запуск classic FlowMind без ML для baseline:

```bash
python experiments/run_experiment.py flowmind \
  --config simulation/new_area/focused.sumocfg \
  --zone simulation/new_area/central_zone.json \
  --duration 900 \
  --no-queue-model
```

У summary потрібно дивитися:

```text
queue_forecast_enabled
queue_forecast_model
queue_forecast_target
queue_forecast_feature_count
queue_forecast_model_count
queue_forecast_horizons
queue_forecast_horizon_weights
queue_forecast_predictions
queue_forecast_failures
phase_out_of_range_skips
clearance_phase_skips
min_green_skips
scoreless_skips
```

ML не повинна мати функції:

```text
set_traffic_light_state()
```

ML тільки прогнозує. Controller вирішує. SafetyValidator дозволяє або
блокує.

## 16. Як не зламати FlowMind при підключенні ML

Потрібен fallback:

```text
якщо model file відсутній
якщо features не зібралися
якщо predict впав
якщо prediction NaN
```

У всіх цих випадках FlowMind має працювати як зараз:

```text
pressure-based controller без прогнозу
```

Тобто ML покращує рішення, але не є єдиною умовою роботи системи.

## 17. Мінімальний критерій готовності ML

Модель можна вважати першою робочою версією, якщо:

- тренування завершується без помилок;
- metadata JSON створюється;
- test MAE/RMSE адекватні;
- test R2 більше 0;
- модель не гірша за попередню версію;
- inference можна виконати швидше, ніж controller decision interval;
- при відсутності моделі система не падає.

## 18. Troubleshooting

### Немає залежностей

Помилка:

```text
Missing ML dependencies
```

Рішення:

```bash
pip install -r requirements.txt
```

### Немає sample CSV

Помилка:

```text
No sample CSV files found
```

Рішення:

```bash
python experiments/run_dataset.py --runs-per-mode 10
```

### Target column not found

Перевірити назви колонок:

```bash
head -1 results/dataset/samples/rivne_focused_local_seed_00042.csv
```

### Не вистачає RAM

Симптом:

```text
Вбито
```

Рішення для 16 GB RAM:

```bash
python experiments/train_queue_model.py --rows-per-file 500
```

Потім поступово збільшувати:

```bash
python experiments/train_queue_model.py --rows-per-file 1000
python experiments/train_queue_model.py --rows-per-file 2000
```

Для швидкого тесту:

```bash
python experiments/train_queue_model.py --max-files 30 --rows-per-file 1000
```

Повний dataset:

```bash
python experiments/train_queue_model.py --full-dataset
```

використовувати тільки коли є багато RAM.

### Метрики погані

Перевірити:

- чи достатньо seed;
- чи є всі режими;
- чи target не занадто далекий;
- чи dataset не містить порожніх запусків;
- чи train/validation/test split має різні seed;
- чи модель не тренується тільки на одному режимі.

Для старту краще:

```bash
python experiments/train_queue_model.py \
  --target target_incoming_queue_60s \
  --modes fixed local flowmind
```

## 19. Рекомендований робочий цикл

1. Зібрати dataset:

```bash
python experiments/run_dataset.py --runs-per-mode 50
```

2. Перевірити dataset:

```bash
python experiments/train_queue_model.py --dry-run
```

3. Натренувати модель:

```bash
python experiments/train_queue_model.py \
  --target target_incoming_queue_60s \
  --output models/queue_lgbm_60s_v1.joblib
```

4. Перевірити metadata:

```bash
cat models/queue_lgbm_60s_v1_metadata.json
```

5. Якщо модель краща за попередню:

```bash
cp models/queue_lgbm_60s_v1.joblib models/queue_lgbm_60s_current.joblib
cp models/queue_lgbm_60s_v1_metadata.json models/queue_lgbm_60s_current_metadata.json
```

6. Тільки після цього підключати до FlowMind controller.

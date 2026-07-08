# 04. Technical Architecture

## Основний pipeline

```text
OpenStreetMap / SUMO map
        ↓
SUMO simulation
        ↓
TraCI connection
        ↓
TrafficStateReader
        ↓
AreaSignalController
        ↓
Signal policy + ML queue forecast + safety validator
        ↓
Traffic light phase changes
        ↓
Metrics + live_status.json + dashboard
```

## Ключові модулі

| Модуль | Роль |
| --- | --- |
| `flowmind/area_model.py` | Опис контрольованої зони, світлофорів, фаз і зв'язків між смугами |
| `flowmind/traffic_state.py` | Збір стану смуг у межах sensor range |
| `flowmind/signal_policy.py` | Оцінка фаз за pressure, demand, downstream і ML forecast |
| `flowmind/controller.py` | Прийняття рішення і застосування фази через TraCI |
| `flowmind/safety_validator.py` | Обмеження, щоб не ламати безпечний порядок фаз |
| `flowmind/queue_forecast.py` | Завантаження LightGBM-моделей і прогноз черги |
| `flowmind/emergency_router.py` | Побудова альтернативних маршрутів швидкої |
| `flowmind/corridor_manager.py` | Green corridor state machine |
| `flowmind/metrics.py` | Збір KPI і live history |
| `api/web_dashboard.py` | FastAPI dashboard, архів, averages, запуск симуляції, Gemini-звіт |

## Як контролер приймає рішення

Кожні `decision_interval` секунд контролер читає стан зони і оцінює доступні green phases.

Фактори:

- incoming queue;
- incoming vehicle count;
- incoming occupancy;
- outgoing queue;
- outgoing occupancy;
- outgoing free slots;
- area pressure;
- ML queue forecast;
- demand wait bonus;
- empty approach penalty;
- emergency priority override;
- min/max green;
- yellow/all-red safety constraints.

Спрощена формула:

```text
phase score =
  current queue pressure
  + area pressure
  + queue forecast
  + demand wait bonus
  + emergency priority
  - downstream blockage penalty
  - empty phase penalty
```

## Чому FlowMind не просто "дає зелений найбільшій черзі"

Якщо після перехрестя вже немає місця, випускати туди новий потік небезпечно: можна заблокувати перехрестя. Тому FlowMind враховує outgoing occupancy і free slots.

Приклад:

```text
Напрямок A має чергу 12 авто, але після перехрестя ділянка забита.
Напрямок B має чергу 8 авто, але після перехрестя є місце.

Локальний контролер може вибрати A.
FlowMind може вибрати B або не продовжити A, бо A створить downstream-блокування.
```

## Безпека фаз

Система не генерує довільні світлофорні стани. Вона працює з існуючою програмою світлофора:

- не перестрибує yellow/all-red фази;
- не перемикає фазу раніше min green;
- не продовжує зелену фазу понад max green;
- використовує штатну тривалість фаз SUMO як базу;
- emergency override проходить через `SafetyValidator`.

## Веб-частина

FastAPI dashboard дає:

- запуск симуляції з параметрами;
- `fixed + FlowMind` baseline;
- live KPI;
- графік історії;
- архів результатів;
- сторінку середніх значень;
- блок швидкої;
- Gemini/AI summary після симуляції;
- fallback summary без Gemini, якщо API недоступний.

## Docker/server

Серверний варіант:

```text
nginx / existing reverse proxy
        ↓
flowmind-web container
        ↓
FastAPI dashboard
        ↓
SUMO headless simulation
        ↓
persistent volume /app/results
```

Ключові файли:

- `Dockerfile`
- `compose.yaml`
- `compose.server.yaml`
- `requirements.runtime.txt`
- `.env` на сервері для `GEMINI_API_KEY`


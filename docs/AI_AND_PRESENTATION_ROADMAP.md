# Подальший розвиток FlowMind Rivne

## Основний принцип

LLM не повинна напряму перемикати світлофори. Реальне зональне керування
мають виконувати локальний оптимізатор і safety layer. LLM потрібна для
диспетчеризації, пояснень і роботи з неструктурованими командами.

```text
SUMO / датчики
      ↓
Traffic State → прогноз 30–90 с
      ↓
Max Pressure / MPC Controller
      ↓
Safety Validator
      ↓
TraCI → світлофори
          ↑
Emergency Router → Green Corridor Manager

LLM → команди диспетчера, пояснення, звіти
```

## Наступні кроки

1. Виділити реальну компактну зону з 4–6 пов’язаних світлофорів.
2. Згенерувати сфокусований трафік, що проходить саме через цю зону.
3. Додати сценарії звичайного трафіку, ранкового перевантаження, ДТП або
   перекриття та появи швидкої.
4. Реалізувати зелений коридор.
5. Провести по 10–20 прогонів кожного режиму з однаковими seed.
6. Показувати median і p95 для часу проїзду, черги та ETA швидкої.

## Рекомендовані моделі

| Задача | Рекомендований підхід |
| --- | --- |
| Прогноз черг на 30–90 секунд | LightGBM або XGBoost |
| Зональне керування | Max Pressure + MPC |
| Пошук маршруту швидкої | Time-dependent A* + Yen K-shortest paths |
| Зелений коридор | Детермінований Corridor State Machine |
| Майбутній research-рівень | MAPPO або інший multi-agent RL |
| Голосовий або текстовий диспетчер | LLM через API або локально |

Для MVP рекомендована комбінація:

```text
LightGBM forecast
        +
Area Pressure Controller
        +
MPC на 60 секунд уперед
```

## Зелений коридор

1. Швидка надсилає поточну позицію і лікарню призначення.
2. Система будує 3–5 маршрутів.
3. Для кожного маршруту оцінює:

```text
вартість =
прогнозований час проїзду
+ затори
+ час підготовки світлофорів
+ вплив на звичайний трафік
```

4. Обирає найкращий маршрут.
5. Для кожного світлофора розраховує ETA швидкої.
6. Запускає керований цикл:

```text
NORMAL → PREPARE → GREEN_WINDOW → CLEARANCE → RECOVERY
```

Зелена хвиля повинна рухатися попереду швидкої, з обов’язковими жовтими
та all-red інтервалами. Не можна перемикати всі світлофори на зелений
одночасно.

Майбутні модулі:

```text
flowmind/
├── emergency_router.py
├── corridor_manager.py
├── traffic_forecast.py
├── mpc_controller.py
└── safety_validator.py
```

## Локальна модель

Для прогнозу та керування LLM не потрібна: LightGBM працює на CPU, а
MPC/Max Pressure — локально без інтернету.

Для локального диспетчерського асистента можна використати
`gpt-oss-20b` через Ollama або vLLM. За офіційними вимогами модель
потребує близько 16 ГБ пам’яті; `gpt-oss-120b` — близько 80 ГБ.

Джерело:
[Introducing gpt-oss](https://openai.com/index/introducing-gpt-oss/).

## OpenAI API

Практична конфігурація:

- `gpt-5.4-mini` — основний швидший і дешевший асистент;
- `gpt-5.5` — складне планування та максимально якісна демонстрація.

Використовувати потрібно Responses API, function calling і Structured
Outputs:

- [Models](https://developers.openai.com/api/docs/models)
- [Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses)
- [Function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)

Безпечні інструменти для LLM:

```text
get_area_state()
calculate_emergency_routes()
simulate_corridor_plan()
request_corridor_activation()
explain_controller_decision()
generate_incident_report()
```

LLM не повинна отримувати прямий інструмент `set_traffic_light_state()`.
Її план спочатку перевіряє `SafetyValidator`, і лише потім локальний
контролер застосовує допустимі фази.

## Сценарій презентації

1. 30 секунд — проблема локального керування.
2. 45 секунд — Fixed Mode і поява затору.
3. 45 секунд — Local Adaptive переносить затор далі.
4. 60 секунд — FlowMind балансує всю зону.
5. 90 секунд — з’являється швидка, система показує альтернативні
   маршрути й створює рухомий зелений коридор.
6. 30 секунд — порівняння travel time, waiting time, maximum queue,
   throughput, emergency ETA та civil traffic penalty.

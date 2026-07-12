# Архітектура FlowMind Rivne pre-MVP

## Шари системи

SUMO не є частиною доменної логіки FlowMind. У pre-MVP він дає
топологію, traffic state і TLS programs через окремі адаптери. У
production ці адаптери можуть бути замінені на API дорожніх
контролерів, GIS і sensor backend.

```text
SUMO / field controllers / backend API
                 |
                 v
        source-specific adapters
                 |
                 v
 TrafficState + TlsSafetyCatalog
                 |
                 v
 FlowMind policy + safety + area controller
                 |
                 v
       command/output adapter
```

`flowmind/tls_safety.py` не імпортує SUMO або TraCI. SUMO-специфічне
перетворення зосереджене у `flowmind/sumo_tls_adapter.py`.

## Потік даних pre-MVP

```text
central_zone.json
        |
        +--> focused route generator --> focused.rou.xml
        |
        v
SUMO network + focused trips
        |
        v
TraCI simulation connection
        |
        +--> AreaModel (автовибір 4-6 світлофорів)
        |
        +--> TrafficStateReader (черги, швидкість, occupancy, місткість)
        |
        v
Signal Policy
  - Static fixed: детермінована fixed-time програма
  - SUMO actuated: окремий simulation baseline
  - Local: тільки вхідна черга конкретного перехрестя
  - FlowMind: вхідна черга + стан наступної ділянки
        |
        v
AreaSignalController
  - продовжує поточний зелений або переходить до наступної фази
  - не перестрибує через жовті/all-red фази
        |
        +--> MetricsCollector --> results/*.csv, *.json
        |
        +--> Streamlit dashboard

emergency.json
        |
        v
EmergencyVehicleManager
  - створює тип швидкої
  - будує маршрут через TraCI
  - планує час виїзду
        |
        +--> Priority Flow hook
        +--> departure / arrival / ETA metrics
```

## Модулі

| Модуль | Відповідальність |
| --- | --- |
| `flowmind/area_model.py` | Читає SUMO network, знаходить керовані світлофори й формує зону |
| `flowmind/tls_safety.py` | Нейтральна модель TLS plans, конфліктна матриця і fail-fast validation |
| `flowmind/sumo_tls_adapter.py` | Перетворює SUMO topology/right-of-way і TraCI Logic на `TlsSafetyCatalog` |
| `flowmind/traffic_state.py` | Нормалізує телеметрію смуг із TraCI |
| `flowmind/signal_policy.py` | Рахує оцінки фаз для Local та FlowMind |
| `flowmind/controller.py` | Застосовує рішення з min/max green та безпечним порядком фаз |
| `flowmind/priority_flow.py` | Підсилює фазу для вказаного пріоритетного авто |
| `flowmind/metrics.py` | Збирає часові ряди й агреговані KPI |
| `flowmind/experiment.py` | Запускає SUMO та координує один експеримент |
| `flowmind/emergency_vehicle.py` | Створює швидку, маршрут і планує її виїзд |
| `dashboard/app.py` | Порівнює режими за збереженими результатами |
| `tools/generate_focused_traffic.py` | Знаходить наскрізні маршрути, перевіряє зв’язність зони та генерує попит |
| `tools/build_sumo_map.py` | Автоматично конвертує локальний OSM-файл у SUMO network, polygons, маршрути, config і manifest |

## Центральна зона

За замовчуванням використовується конфігурація
`simulation/rivne_area/central_zone.json`. Вона містить шість реальних
перехресть центрального Рівного:

- чотири послідовні світлофори коридору Соборної;
- три світлофори коридору В’ячеслава Чорновола;
- спільний вузол двох коридорів на перетині Соборної та Чорновола.

Генератор шукає вуличні входи й виходи на відстані 1.2–2.2 км від центру
зони, будує найкоротші допустимі автомобільні маршрути та залишає лише ті,
що проходять щонайменше через два контрольовані світлофори. Згенерований
manifest додатково підтверджує покриття всіх шести вузлів і зв’язність
маршрутного графа.

## Межі pre-MVP

- Контролюється компактна центральна зона на повній мапі Рівного; окрему
  обрізану SUMO-мережу ще не створено.
- Контролер використовує пояснювану pressure-based евристику, а не навчену нейромережу.
- Автоматична швидка та один маршрут до лікарні готові; побудова й
  порівняння 3–5 альтернативних маршрутів ще не реалізовані.
- Throughput і travel time рахуються глобально для однакового сценарію, а черги, очікування, зупинки та gridlock risk — для керованої зони.

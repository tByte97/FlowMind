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
 Directed AreaGraph + AreaDecisionSnapshot
                 |
                 v
 FlowMind policy + safety + area controller
                 |
                 v
       command/output adapter
```

`flowmind/tls_safety.py` не імпортує SUMO або TraCI. SUMO-специфічне
перетворення зосереджене у `flowmind/sumo_tls_adapter.py`.
Так само `flowmind/zone_graph.py` не залежить від SUMO; шляхи edges/lanes
для pre-MVP готує `flowmind/sumo_zone_graph_adapter.py`.
Emergency corridor та recovery offset також нейтральні. Лише
`flowmind/sumo_corridor_adapter.py` читає `vehicle.getNextTLS` і перетворює
його на upcoming/passed TLS events. У production цей adapter може бути
замінений на GPS/V2X/backend event source.

## Зональне рішення

Corridors з `central_zone.json` розгортаються у directed segments для
обох напрямків. Кожен segment зберігає:

- upstream/downstream TLS;
- проміжні edges і lanes;
- довжину та storage capacity;
- occupancy, queue, free slots і spillback probability.

На кожному control tick система спочатку формує один
`AreaDecisionSnapshot` для всіх TLS. Downstream risk передається на
1–2 наступні перехрестя лише вздовж directed graph. Усі рішення
обчислюються до першої TraCI-команди, тому порядок TLS у циклі
не змінює зональний вибір.

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
  - FlowMind: вхідна черга + zonal graph + shadow ML forecast
        |
        v
AreaSignalController
  - продовжує поточний зелений або переходить до наступної фази
  - не перестрибує через жовті/all-red фази
        |
        +--> MetricsCollector --> results/*.csv, *.json
        |
        +--> FastAPI dashboard

emergency.json
        |
        v
EmergencyVehicleManager
  - створює тип швидкої
  - обирає 3–5 маршрутних альтернатив
  - повторно оцінює маршрут перед departure
  - планує час виїзду
        |
        +--> PREPARE (soft look-ahead for 3 TLS)
        +--> GREEN_WINDOW (hard priority + downstream gate)
        +--> confirmed passage -> clearance -> phase/offset recovery
        +--> departure / arrival / ETA + measured civilian impact
```

## Модулі

| Модуль | Відповідальність |
| --- | --- |
| `flowmind/area_model.py` | Читає SUMO network, знаходить керовані світлофори й формує зону |
| `flowmind/tls_safety.py` | Нейтральна модель TLS plans, конфліктна матриця і fail-fast validation |
| `flowmind/sumo_tls_adapter.py` | Перетворює SUMO topology/right-of-way і TraCI Logic на `TlsSafetyCatalog` |
| `flowmind/zone_graph.py` | Нейтральний zonal graph, storage/spillback state і спільний decision snapshot |
| `flowmind/sumo_zone_graph_adapter.py` | Знаходить SUMO road paths між сусідніми TLS з corridor definition |
| `flowmind/traffic_state.py` | Нормалізує телеметрію смуг із TraCI |
| `flowmind/signal_policy.py` | Рахує оцінки фаз для Local та FlowMind |
| `flowmind/controller.py` | Застосовує рішення з min/max green та безпечним порядком фаз |
| `flowmind/priority_flow.py` | Підсилює фазу для вказаного пріоритетного авто |
| `flowmind/queue_forecast.py` | Current-policy/counterfactual contract, capacity bounds, OOD/confidence і artifact provenance |
| `flowmind/corridor_manager.py` | Нейтральний state machine PREPARE/GREEN_WINDOW/CLEARANCE/RECOVERY |
| `flowmind/sumo_corridor_adapter.py` | Підтверджує stop-line passage з SUMO telemetry |
| `flowmind/corridor_recovery.py` | Відновлює базову фазу та cycle offset |
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
- ML-моделі навмисно залишаються у shadow mode, доки парна оцінка на
  більшій кількості seed не пройде regression gates.
- TLS на emergency-маршруті, що не пройшов startup safety validation,
  не керується FlowMind і залишається на штатній TLS-програмі.
- Throughput і travel time рахуються глобально для однакового сценарію, а черги, очікування, зупинки та gridlock risk — для керованої зони.

Так, це **реально зробити на Python**. Найкращий шлях: не писати власний симулятор, а взяти **SUMO** як транспортний движок, а Python використати як “мозок”, який читає стан руху й керує світлофорами.

## Базовий стек для FlowMind Rivne

### 1. Симулятор

Головний інструмент: **Eclipse SUMO**.

SUMO — це open-source мікроскопічний симулятор міського руху: він моделює окремі автомобілі, дороги, маршрути, світлофори, пішоходів і може працювати з великими мережами. Для вашої ідеї це майже ідеальний варіант. ([Eclipse SUMO][1])

Вам знадобляться програми з пакета SUMO:

| Інструмент         | Для чого                                        |
| ------------------ | ----------------------------------------------- |
| **sumo-gui**       | красива візуальна симуляція для презентації     |
| **netconvert**     | конвертація OpenStreetMap у SUMO-мережу         |
| **netedit**        | ручне редагування перехресть, світлофорів, смуг |
| **duarouter**      | побудова маршрутів для машин                    |
| **randomTrips.py** | генерація тестового трафіку                     |
| **TraCI**          | керування симуляцією з Python                   |

SUMO офіційно підтримує імпорт дорожніх мереж з OpenStreetMap, тому можна взяти реальну ділянку Рівного, конвертувати її в `.net.xml` і запускати симуляцію. ([Eclipse SUMO][2])

## 2. Керування через Python

Так, Python тут підходить повністю.

SUMO запускається як сервер симуляції, а Python-скрипт підключається до нього через **TraCI**. TraCI дозволяє читати стан симуляції та надсилати команди назад: змінювати фази світлофорів, тривалість фаз, маршрути машин і поведінку транспорту. Офіційний туторіал SUMO прямо показує adaptive traffic lights на Python. ([Eclipse SUMO][3])

Схема буде така:

```text
SUMO / sumo-gui
    ↓ стан машин, черги, швидкість, світлофори
Python FlowMind Controller
    ↓ рішення: кому дати зелений, кого притримати
SUMO / sumo-gui
    ↓ новий стан області
Dashboard / метрики / презентація
```

## Python-бібліотеки

### Обов’язкові

```txt
traci
sumolib
numpy
pandas
networkx
lxml
```

| Бібліотека   | Навіщо                                                          |
| ------------ | --------------------------------------------------------------- |
| **traci**    | керування SUMO з Python                                         |
| **sumolib**  | робота з SUMO-мережами, XML, ребрами, світлофорами              |
| **numpy**    | розрахунки, матриці, індекси завантаження                       |
| **pandas**   | метрики, результати експериментів                               |
| **networkx** | представити область як граф: перехрестя = вузли, дороги = ребра |
| **lxml**     | читання/зміна `.xml` файлів SUMO                                |

`sumolib` — це офіційний набір Python-модулів для роботи з SUMO-мережами, виходами симуляції та іншими артефактами SUMO. ([Eclipse SUMO][4])

### Для красивої презентації

```txt
streamlit
plotly
folium
pydeck
matplotlib
```

| Бібліотека     | Для чого                                   |
| -------------- | ------------------------------------------ |
| **Streamlit**  | швидкий веб-дашборд на Python              |
| **Plotly**     | графіки, KPI, анімовані діаграми           |
| **Folium**     | інтерактивна карта на основі Leaflet       |
| **PyDeck**     | гарні карти, шари, heatmap, рух точок      |
| **Matplotlib** | прості графіки для порівняння before/after |

Streamlit добре підходить, бо дозволяє швидко зробити інтерактивний Python-додаток без фронтенду. ([docs.streamlit.io][5]) Folium зручний для інтерактивних карт, коли дані готуються в Python. ([python-visualization.github.io][6])

## Як зробити симуляцію красивою

Є два варіанти.

### Варіант 1: SUMO-GUI + дашборд

Найреалістичніше для двох студентів.

На презентації показуєте:

* ліворуч `sumo-gui` з рухом машин;
* праворуч Streamlit-дашборд;
* зверху кнопки сценаріїв: **Fixed**, **Local Adaptive**, **FlowMind Area Balance**, **Emergency Priority**;
* внизу метрики: середній час проїзду, черга, зупинки, ризик блокування.

`sumo-gui` офіційно призначений для візуального запуску симуляцій, а SUMO також має інструменти для візуалізації результатів. ([Eclipse SUMO][7])

### Варіант 2: своя веб-візуалізація

Це красивіше, але складніше.

SUMO може експортувати **FCD output** — координати, швидкість та інші дані кожного автомобіля на кожному кроці симуляції. Це можна перетворити в анімовану карту на Plotly/PyDeck. ([Eclipse SUMO][8])

Для хакатону я б робив так:

> Основна симуляція — у SUMO-GUI, а красивий шар для суддів — у Streamlit: карта області, heatmap завантаження, статус світлофорів і графіки покращення.

## Яка “модель” потрібна

Тут важливо: **YOLO не потрібна**, бо ми не працюємо з відео. У симуляції ми й так знаємо, де машини, яка їхня швидкість, скільки їх у черзі і на якій смузі вони стоять.

Для MVP вам не потрібна велика нейромережа. Потрібна **модель прийняття рішень**.

Я б зробив три рівні:

## 1. Simulation model

Це сам SUMO.

Він моделює:

* автомобілі;
* смуги;
* маршрути;
* затори;
* світлофори;
* прискорення/гальмування;
* поведінку машин на дорозі.

SUMO вже є мікроскопічною моделлю руху, тому не треба писати фізику автомобілів самостійно. ([Eclipse SUMO][1])

## 2. Traffic state model

Це ваша внутрішня модель області.

Наприклад:

```python
area_state = {
    "intersection_A": {
        "north_queue": 12,
        "south_queue": 5,
        "east_queue": 18,
        "west_queue": 3,
        "downstream_blocked": False
    },
    "intersection_B": {
        "incoming_queue": 20,
        "outgoing_capacity": 7
    }
}
```

Але краще мислити як граф:

```text
Вузли = світлофорні перехрестя
Ребра = дороги між ними
Вага ребра = черга, швидкість, щільність, затримка
```

Для цього підійде `networkx`.

## 3. Control model

Це головний “AI”.

Для двох студентів я рекомендую не починати з reinforcement learning. Краще зробити **Area Pressure Balancing Controller**.

Ідея:

```text
Тиск дороги = черга перед перехрестям - вільне місце після перехрестя
```

AI не просто дає зелений там, де найбільше машин. Він перевіряє:

* чи є місце після перехрестя;
* чи не заблокується наступне перехрестя;
* чи не накопичується затор у центрі області;
* чи не краще тимчасово стримати в’їзд;
* який світлофор треба перемкнути першим;
* яку фазу продовжити або скоротити.

SUMO вже має базові типи світлофорів: `static`, `actuated`, `delay_based`, але ваша цінність буде в тому, що ви робите **координацію області**, а не лише стандартний адаптивний світлофор. ([Eclipse SUMO][9])

## Мінімальна логіка FlowMind

Кожні 5 або 10 секунд Python робить:

```text
1. Зчитати стан усіх доріг у зоні
2. Порахувати черги й завантаженість
3. Визначити найгірші ділянки
4. Перевірити, куди можна безпечно випускати потік
5. Вибрати фази світлофорів
6. Застосувати рішення через TraCI
7. Записати метрики
```

TraCI дозволяє керувати світлофорами під час активної симуляції, тобто змінювати їхній стан не до запуску, а прямо в процесі. ([Eclipse SUMO][10])

## Чи потрібен reinforcement learning

Можна, але не для першого MVP.

Є бібліотека **SUMO-RL**, яка дає середовище для reinforcement learning у задачах traffic signal control і сумісна з популярними RL-бібліотеками. ([lucasalegre.github.io][11]) Stable-Baselines3 дає готові реалізації RL-алгоритмів на PyTorch. ([stable-baselines3.readthedocs.io][12])

Але для хакатону це ризиковано:

* треба багато тренувати;
* результат може бути нестабільним;
* складніше пояснити;
* важче дебажити;
* можна не встигнути.

Тому краще так:

| Версія           | Модель                                           |
| ---------------- | ------------------------------------------------ |
| MVP              | Area Pressure Balancing, без нейромережі         |
| Покращена версія | MPC / оптимізація на 30-90 секунд вперед         |
| Research-версія  | Multi-agent reinforcement learning через SUMO-RL |

## Оптимальний стек для вас

Я б обрав так:

```txt
SUMO
sumo-gui
netedit
netconvert
duarouter
randomTrips.py

Python 3.11+
traci
sumolib
numpy
pandas
networkx
streamlit
plotly
folium
lxml
```

Опційно:

```txt
osmnx
ortools
sumo-rl
stable-baselines3
```

OSMnx може завантажувати й аналізувати дорожні мережі з OpenStreetMap напряму в Python, але для першого MVP можна обійтися інструментами SUMO. ([osmnx.readthedocs.io][13]) OR-Tools корисний, якщо ви захочете додати маршрутизацію або оптимізацію з обмеженнями, але для керування світлофорами він не обов’язковий. ([Google for Developers][14])

## Структура проєкту

```text
flowmind-rivne/
├── data/
│   ├── rivne.osm.xml
│   ├── area.net.xml
│   ├── routes.rou.xml
│   └── detectors.add.xml
│
├── simulation/
│   ├── flowmind.sumocfg
│   ├── fixed.sumocfg
│   └── scenarios/
│
├── flowmind/
│   ├── controller.py
│   ├── area_model.py
│   ├── traffic_state.py
│   ├── signal_policy.py
│   ├── metrics.py
│   └── emergency.py
│
├── dashboard/
│   └── app.py
│
├── experiments/
│   ├── run_fixed.py
│   ├── run_local_adaptive.py
│   └── run_flowmind.py
│
└── requirements.txt
```

## Що показувати на презентації

Три режими:

### 1. Fixed mode

Світлофори працюють по фіксованому таймеру.

### 2. Local adaptive mode

Кожне перехрестя дивиться лише на себе.

Це треба показати спеціально, бо тут буде видно проблему: одне перехрестя розвантажилось, але затор переїхав далі.

### 3. FlowMind Area Balance

AI бачить всю область і балансує її.

Метрики:

```text
Середній час проїзду через область
Середня довжина черги
Максимальна черга на одному напрямку
Кількість зупинок
Throughput
Ризик блокування перехрестя
Час проїзду швидкої
```

## Найкраща MVP-версія

Для двох студентів я б зробив так:

> **Одна ділянка Рівного з 4-6 світлофорними перехрестями. FlowMind порівнює фіксоване керування, локальне адаптивне керування і зональне балансування області. Додатково є сценарій швидкої, який працює поверх уже збалансованої системи.**

Тобто ядро:

```text
Area Flow Balancing
```

А зелений коридор:

```text
Priority Override Scenario
```

## Що НЕ брати на MVP

Не варто зараз брати:

* YOLO;
* реальні камери;
* мобільний застосунок;
* повний RL;
* весь транспорт Рівного;
* реальні API світлофорів;
* інтеграцію з Waze;
* 3D-візуалізацію з Unity.

Це все можна написати як future development, але не робити.

## Висновок

Так, це можна зробити через Python.

Найкраща технічна формула:

> **SUMO симулює місто, Python через TraCI керує світлофорами, NetworkX представляє область як граф, Streamlit/Plotly показують красивий дашборд, а FlowMind Controller приймає рішення для балансування всієї зони.**

Для MVP вам не потрібна нейромережа. Достатньо сильного AI/optimization controller, який реально змінює стан симуляції й доводить результат метриками.

[1]: https://sumo.dlr.de/docs/index.html?utm_source=chatgpt.com "SUMO Documentation"
[2]: https://sumo.dlr.de/docs/Networks/Import/OpenStreetMap.html?utm_source=chatgpt.com "OpenStreetMap - SUMO Documentation"
[3]: https://sumo.dlr.de/docs/TraCI/index.html?utm_source=chatgpt.com "TraCI - SUMO Documentation"
[4]: https://sumo.dlr.de/docs/Tools/Sumolib.html?utm_source=chatgpt.com "Sumolib - SUMO Documentation"
[5]: https://docs.streamlit.io/?utm_source=chatgpt.com "Streamlit documentation"
[6]: https://python-visualization.github.io/folium/?utm_source=chatgpt.com "Folium 0.20.0 documentation"
[7]: https://sumo.dlr.de/docs/sumo-gui.html?utm_source=chatgpt.com "sumo-gui - SUMO Documentation"
[8]: https://sumo.dlr.de/docs/Simulation/Output/FCDOutput.html?utm_source=chatgpt.com "FCDOutput - SUMO Documentation"
[9]: https://sumo.dlr.de/docs/Simulation/Traffic_Lights.html?utm_source=chatgpt.com "Traffic Lights - SUMO Documentation"
[10]: https://sumo.dlr.de/docs/Tutorials/TraCI4Traffic_Lights.html?utm_source=chatgpt.com "TraCI4Traffic Lights - SUMO Documentation"
[11]: https://lucasalegre.github.io/sumo-rl/?utm_source=chatgpt.com "SUMO-RL 1.4.5 documentation"
[12]: https://stable-baselines3.readthedocs.io/?utm_source=chatgpt.com "Stable-Baselines3 Docs - Reliable Reinforcement Learning ..."
[13]: https://osmnx.readthedocs.io/?utm_source=chatgpt.com "OSMnx 2.1.0 documentation"
[14]: https://developers.google.com/optimization/routing/vrp?utm_source=chatgpt.com "Vehicle Routing Problem | OR-Tools"

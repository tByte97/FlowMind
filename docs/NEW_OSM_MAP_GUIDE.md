# Створення нової SUMO-карти з OpenStreetMap

Інструкція описує актуальний процес для FlowMind Rivne та SUMO 1.27.1:

1. автоматично через OSM Web Wizard;
2. автоматично через командний рядок;
3. вручну через OpenStreetMap, `netconvert`, `polyconvert` і
   `randomTrips.py`;
4. ручне редагування мережі в `netedit`;
5. підключення нової карти до FlowMind;
6. налаштування центральної зони та швидкої.

Офіційна документація:

- [OSM Web Wizard](https://sumo.dlr.de/docs/Tutorials/OSMWebWizard.html)
- [Імпорт OpenStreetMap](https://sumo.dlr.de/docs/Networks/Import/OpenStreetMap.html)
- [netconvert](https://sumo.dlr.de/docs/netconvert.html)
- [polyconvert](https://sumo.dlr.de/docs/polyconvert.html)
- [randomTrips.py](https://sumo.dlr.de/docs/Tools/Trip.html)

## 1. Перед початком

Відкрити термінал у корені FlowMind і активувати `.venv`.

### Windows PowerShell

```powershell
Set-Location C:\Projects\FlowMind
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
$env:SUMO_HOME = python -c "import sumo; print(sumo.SUMO_HOME)"
$env:Path = "$env:SUMO_HOME\bin;$env:Path"
$SUMO_TOOLS = Join-Path $env:SUMO_HOME "tools"
```

Перевірити:

```powershell
sumo --version
netconvert --version
polyconvert --version
netedit --version
Write-Output $env:SUMO_HOME
```

### Linux / macOS

```bash
cd /path/to/FlowMind
source .venv/bin/activate
export SUMO_HOME="$(python -c 'import sumo; print(sumo.SUMO_HOME)')"
export PATH="$SUMO_HOME/bin:$PATH"
SUMO_TOOLS="$SUMO_HOME/tools"
```

Перевірити:

```bash
sumo --version
netconvert --version
polyconvert --version
netedit --version
echo "$SUMO_HOME"
```

## 2. Як вибрати область

SUMO використовує bounding box у порядку:

```text
west,south,east,north
```

Тобто:

```text
мінімальна довгота,
мінімальна широта,
максимальна довгота,
максимальна широта
```

Приклад компактної центральної частини Рівного:

```text
26.235,50.607,26.275,50.637
```

Отримати координати можна на:

<https://www.openstreetmap.org/export>

Порядок дій:

1. відкрити OpenStreetMap;
2. знайти місто;
3. натиснути Export;
4. натиснути Manually select a different area;
5. виділити потрібну область;
6. записати Left, Bottom, Right, Top;
7. скласти `west,south,east,north`.

Для MVP не потрібно завантажувати все місто. Рекомендована зона:

- приблизно 2–5 км по ширині;
- 4–10 світлофорних перехресть;
- дві або три основні вулиці;
- щонайменше два наскрізні транспортні коридори.

## 3. Варіант A — автоматично через OSM Web Wizard

Це найпростіший спосіб створити новий сценарій.

### Windows PowerShell

Задати назву:

```powershell
$MAP = "new_area"
$BBOX = "26.235,50.607,26.275,50.637"
```

Запустити Wizard:

```powershell
python "$SUMO_TOOLS\osmWebWizard.py" `
  --bbox $BBOX `
  --output "simulation\$MAP" `
  --begin 0 `
  --end 1800
```

### Linux / macOS

```bash
MAP="new_area"
BBOX="26.235,50.607,26.275,50.637"
```

```bash
python "$SUMO_TOOLS/osmWebWizard.py" \
  --bbox "$BBOX" \
  --output "simulation/$MAP" \
  --begin 0 \
  --end 1800
```

Wizard відкривається у браузері.

У ньому потрібно:

1. перевірити виділену область;
2. залишити тип транспорту `passenger`;
3. задати тривалість 1800 або 3600 секунд;
4. увімкнути polygons для будівель;
5. залишити стандартні traffic lights;
6. натиснути Generate Scenario;
7. дочекатися відкриття SUMO GUI.

Типовий результат:

```text
simulation/new_area/
├── osm.net.xml.gz
├── osm.poly.xml.gz
├── osm.passenger.trips.xml
├── osm.sumocfg
├── osm.view.xml
├── osm.netccfg
└── osm.polycfg
```

Перевірити сценарій:

### Windows

```powershell
sumo -c simulation\new_area\osm.sumocfg --end 120
sumo-gui -c simulation\new_area\osm.sumocfg
```

### Linux / macOS

```bash
sumo -c simulation/new_area/osm.sumocfg --end 120
sumo-gui -c simulation/new_area/osm.sumocfg
```

## 4. Варіант B — повністю автоматично через CLI

Цей варіант не потребує ручної роботи у Web Wizard.

Він використовує:

```text
osmGet.py
    ↓
osmBuild.py
    ↓
randomTrips.py
    ↓
SUMO
```

### Windows PowerShell

Створити папку:

```powershell
$MAP = "new_area"
$BBOX = "26.235,50.607,26.275,50.637"
New-Item -ItemType Directory -Force "simulation\$MAP"
Set-Location "simulation\$MAP"
```

Завантажити OSM:

```powershell
python "$SUMO_TOOLS\osmGet.py" `
  --bbox $BBOX `
  --prefix osm `
  --output-dir . `
  --shapes `
  --verbose
```

Зібрати SUMO-мережу та polygons:

```powershell
python "$SUMO_TOOLS\osmBuild.py" `
  --osm-file osm.osm.xml `
  --prefix osm `
  --vehicle-classes passenger `
  --typemap "$env:SUMO_HOME\data\typemap\osmPolyconvert.typ.xml" `
  --netconvert-typemap "$env:SUMO_HOME\data\typemap\osmNetconvert.typ.xml,$env:SUMO_HOME\data\typemap\osmNetconvertUrbanDe.typ.xml" `
  --output-directory . `
  --gzip `
  --verbose
```

Створити маршрути:

```powershell
python "$SUMO_TOOLS\randomTrips.py" `
  --net-file osm.net.xml.gz `
  --output-trip-file osm.passenger.trips.xml `
  --route-file osm.passenger.rou.xml `
  --begin 0 `
  --end 1800 `
  --insertion-rate 2400 `
  --vehicle-class passenger `
  --edge-permission passenger `
  --min-distance 300 `
  --fringe-factor 5 `
  --remove-loops `
  --validate `
  --seed 42 `
  --trip-attributes 'departLane="best" departSpeed="max"'
```

Повернутися в корінь:

```powershell
Set-Location ..\..
```

### Linux / macOS

```bash
MAP="new_area"
BBOX="26.235,50.607,26.275,50.637"
mkdir -p "simulation/$MAP"
cd "simulation/$MAP"
```

```bash
python "$SUMO_TOOLS/osmGet.py" \
  --bbox "$BBOX" \
  --prefix osm \
  --output-dir . \
  --shapes \
  --verbose
```

```bash
python "$SUMO_TOOLS/osmBuild.py" \
  --osm-file osm.osm.xml \
  --prefix osm \
  --vehicle-classes passenger \
  --typemap "$SUMO_HOME/data/typemap/osmPolyconvert.typ.xml" \
  --netconvert-typemap "$SUMO_HOME/data/typemap/osmNetconvert.typ.xml,$SUMO_HOME/data/typemap/osmNetconvertUrbanDe.typ.xml" \
  --output-directory . \
  --gzip \
  --verbose
```

```bash
python "$SUMO_TOOLS/randomTrips.py" \
  --net-file osm.net.xml.gz \
  --output-trip-file osm.passenger.trips.xml \
  --route-file osm.passenger.rou.xml \
  --begin 0 \
  --end 1800 \
  --insertion-rate 2400 \
  --vehicle-class passenger \
  --edge-permission passenger \
  --min-distance 300 \
  --fringe-factor 5 \
  --remove-loops \
  --validate \
  --seed 42 \
  --trip-attributes 'departLane="best" departSpeed="max"'
```

```bash
cd ../..
```

### Що означає інтенсивність

```text
--insertion-rate 2400
```

означає приблизно 2400 автомобілів на годину для всієї мережі.

Приклади:

```text
600   — слабкий трафік
1200  — середній трафік
2400  — високий трафік
3600  — стрес-тест
```

Для великої карти 2400 може бути мало. Для маленької — занадто багато.
Параметр потрібно калібрувати експериментально.

## 5. Варіант C — вручну з OpenStreetMap

### 5.1 Завантаження через сайт

Відкрити:

<https://www.openstreetmap.org/export>

Виділити область, натиснути Export і зберегти файл як:

```text
osm_bbox.osm.xml
```

Перемістити його в:

```text
simulation/new_area/osm_bbox.osm.xml
```

### 5.2 Завантаження малої області командою

OpenStreetMap API підходить лише для невеликих областей.

#### Windows PowerShell

```powershell
$MAP = "new_area"
$BBOX = "26.235,50.607,26.275,50.637"
New-Item -ItemType Directory -Force "simulation\$MAP"
Set-Location "simulation\$MAP"
Invoke-WebRequest `
  -Uri "https://api.openstreetmap.org/api/0.6/map?bbox=$BBOX" `
  -OutFile "osm_bbox.osm.xml"
```

#### Linux / macOS

```bash
MAP="new_area"
BBOX="26.235,50.607,26.275,50.637"
mkdir -p "simulation/$MAP"
cd "simulation/$MAP"
curl -L "https://api.openstreetmap.org/api/0.6/map?bbox=$BBOX" \
  -o osm_bbox.osm.xml
```

Якщо API повертає помилку через розмір області, потрібно використати
`osmGet.py`, Web Wizard або зменшити bounding box.

### 5.3 Ручний netconvert

#### Windows PowerShell

```powershell
netconvert `m_base Dispatcher_Client::request_read_and_idx::timeout. The server is probably too busy to handle your request. 
  --osm-files osm_bbox.osm.xml `
  --output-file osm.net.xml.gz `
  --type-files "$env:SUMO_HOME\data\typemap\osmNetconvert.typ.xml,$env:SUMO_HOME\data\typemap\osmNetconvertUrbanDe.typ.xml" `
  --geometry.remove `
  --roundabouts.guess `
  --ramps.guess `
  --junctions.join `
  --tls.guess-signals `
  --tls.discard-simple `
  --tls.join `
  --tls.default-type actuated `
  --osm.turn-lanes `
  --remove-edges.isolated `
  --output.street-names `
  --output.original-names `
  --verbose
```

#### Linux / macOS

```bash
netconvert \
  --osm-files osm_bbox.osm.xml \
  --output-file osm.net.xml.gz \
  --type-files "$SUMO_HOME/data/typemap/osmNetconvert.typ.xml,$SUMO_HOME/data/typemap/osmNetconvertUrbanDe.typ.xml" \
  --geometry.remove \
  --roundabouts.guess \
  --ramps.guess \
  --junctions.join \
  --tls.guess-signals \
  --tls.discard-simple \
  --tls.join \
  --tls.default-type actuated \
  --osm.turn-lanes \
  --remove-edges.isolated \
  --output.street-names \
  --output.original-names \
  --verbose
```

Рекомендовані опції SUMO:

- `--geometry.remove` — спрощує геометрію;
- `--ramps.guess` — додає ймовірні смуги розгону;
- `--junctions.join` — об’єднує близькі вузли одного перехрестя;
- `--tls.guess-signals` — інтерпретує OSM traffic signals;
- `--tls.discard-simple` — прибирає надто прості світлофори;
- `--tls.join` — об’єднує пов’язані signal nodes;
- `--tls.default-type actuated` — використовує адаптивну базову програму;
- `--osm.turn-lanes` — враховує OSM turn lanes;
- `--output.street-names` — зберігає назви вулиць.

### 5.4 Ручне створення polygons

#### Windows PowerShell

```powershell
polyconvert `
  --net-file osm.net.xml.gz `
  --osm-files osm_bbox.osm.xml `
  --type-file "$env:SUMO_HOME\data\typemap\osmPolyconvert.typ.xml" `
  --output-file osm.poly.xml.gz `
  --osm.keep-full-type `
  --verbose
```

#### Linux / macOS

```bash
polyconvert \
  --net-file osm.net.xml.gz \
  --osm-files osm_bbox.osm.xml \
  --type-file "$SUMO_HOME/data/typemap/osmPolyconvert.typ.xml" \
  --output-file osm.poly.xml.gz \
  --osm.keep-full-type \
  --verbose
```

Polygons потрібні для відображення будівель, води, парків і лікарень.
На логіку руху вони не впливають.

### 5.5 Ручна генерація маршрутів

#### Windows PowerShell

```powershell
python "$SUMO_TOOLS\randomTrips.py" `
  -n osm.net.xml.gz `
  -o osm.passenger.trips.xml `
  -r osm.passenger.rou.xml `
  -b 0 `
  -e 1800 `
  --insertion-rate 2400 `
  --vehicle-class passenger `
  --edge-permission passenger `
  --min-distance 300 `
  --fringe-factor 5 `
  --remove-loops `
  --validate `
  --seed 42 `
  --trip-attributes 'departLane="best" departSpeed="max"'
```

#### Linux / macOS

```bash
python "$SUMO_TOOLS/randomTrips.py" \
  -n osm.net.xml.gz \
  -o osm.passenger.trips.xml \
  -r osm.passenger.rou.xml \
  -b 0 \
  -e 1800 \
  --insertion-rate 2400 \
  --vehicle-class passenger \
  --edge-permission passenger \
  --min-distance 300 \
  --fringe-factor 5 \
  --remove-loops \
  --validate \
  --seed 42 \
  --trip-attributes 'departLane="best" departSpeed="max"'
```

`--validate` запускає `duarouter` і відкидає недоступні поїздки.

## 6. Створення SUMO-конфігурації

У папці карти створити файл:

```text
osm.sumocfg
```

Вміст:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<sumoConfiguration>
    <input>
        <net-file value="osm.net.xml.gz"/>
        <route-files value="osm.passenger.rou.xml"/>
        <additional-files value="osm.poly.xml.gz"/>
    </input>
    <time>
        <begin value="0"/>
        <end value="1800"/>
    </time>
    <processing>
        <ignore-route-errors value="false"/>
        <time-to-teleport value="300"/>
    </processing>
    <report>
        <no-step-log value="true"/>
        <duration-log.statistics value="true"/>
    </report>
</sumoConfiguration>
```

### Створити файл через PowerShell

```powershell
@'
<?xml version="1.0" encoding="UTF-8"?>
<sumoConfiguration>
    <input>
        <net-file value="osm.net.xml.gz"/>
        <route-files value="osm.passenger.rou.xml"/>
        <additional-files value="osm.poly.xml.gz"/>
    </input>
    <time>
        <begin value="0"/>
        <end value="1800"/>
    </time>
    <processing>
        <ignore-route-errors value="false"/>
        <time-to-teleport value="300"/>
    </processing>
    <report>
        <no-step-log value="true"/>
        <duration-log.statistics value="true"/>
    </report>
</sumoConfiguration>
'@ | Set-Content -Encoding UTF8 osm.sumocfg
```

## 7. Перевірка нової карти

Спочатку headless:

### Windows

```powershell
sumo -c osm.sumocfg --end 120 --verbose
```

### Linux / macOS

```bash
sumo -c osm.sumocfg --end 120 --verbose
```

Потім GUI:

```powershell
sumo-gui -c osm.sumocfg
```

Перевірити:

- карта відкривається;
- автомобілі додаються;
- маршрути не мають помилок;
- автомобілі не застрягають одразу після старту;
- світлофори мають коректні фази;
- немає масових teleport;
- немає постійного gridlock.

## 8. Ручне редагування через netedit

Перед редагуванням створити резервну копію.

### Windows

```powershell
Copy-Item osm.net.xml.gz osm.before_edit.net.xml.gz
netedit -s osm.net.xml.gz
```

### Linux / macOS

```bash
cp osm.net.xml.gz osm.before_edit.net.xml.gz
netedit -s osm.net.xml.gz
```

У `netedit` потрібно перевірити:

1. Junction mode — форма і пріоритети перехресть.
2. Traffic Light mode — фази та controlled links.
3. Connection mode — дозволені повороти між смугами.
4. Edge mode — кількість смуг, швидкість і permissions.
5. Demand mode — маршрути, vehicles і flows.

Після редагування:

```text
File → Save Network
```

Якщо змінили edge IDs або топологію, маршрути потрібно згенерувати
заново:

### Windows

```powershell
python "$SUMO_TOOLS\randomTrips.py" `
  -n osm.net.xml.gz `
  -o osm.passenger.trips.xml `
  -r osm.passenger.rou.xml `
  -b 0 `
  -e 1800 `
  --insertion-rate 2400 `
  --vehicle-class passenger `
  --edge-permission passenger `
  --remove-loops `
  --validate `
  --seed 42
```

### Linux / macOS

```bash
python "$SUMO_TOOLS/randomTrips.py" \
  -n osm.net.xml.gz \
  -o osm.passenger.trips.xml \
  -r osm.passenger.rou.xml \
  -b 0 \
  -e 1800 \
  --insertion-rate 2400 \
  --vehicle-class passenger \
  --edge-permission passenger \
  --remove-loops \
  --validate \
  --seed 42
```

## 9. Підключення карти до FlowMind

FlowMind очікує такі імена в папці нової карти:

```text
simulation/new_area/
├── osm.net.xml.gz
├── osm.poly.xml.gz
├── focused.rou.xml
├── focused.sumocfg
├── focused.manifest.json
├── central_zone.json
└── emergency.json
```

### 9.1 Переглянути всі світлофори

З кореня FlowMind.

#### Windows PowerShell

```powershell
python -c "import sumolib; n=sumolib.net.readNet(r'simulation\new_area\osm.net.xml.gz', withPrograms=True, withConnections=True); [print(t.getID(), 'links=', len(t.getConnections()), 'programs=', len(t.getPrograms())) for t in n.getTrafficLights() if t.getPrograms()]"
```

#### Linux / macOS

```bash
python -c "import sumolib; n=sumolib.net.readNet('simulation/new_area/osm.net.xml.gz', withPrograms=True, withConnections=True); [print(t.getID(), 'links=', len(t.getConnections()), 'programs=', len(t.getPrograms())) for t in n.getTrafficLights() if t.getPrograms()]"
```

Вибрати 4–6 пов’язаних світлофорів.

Рекомендовано:

- відкрити мережу в `netedit`;
- увімкнути перегляд junction ID;
- вибрати один основний коридор;
- додати один або два перехресні напрямки;
- уникати ізольованих pedestrian signals.

### 9.2 Створити central_zone.json

Скопіювати шаблон:

#### Windows

```powershell
Copy-Item simulation\rivne_area\central_zone.json simulation\new_area\central_zone.json
notepad simulation\new_area\central_zone.json
```

#### Linux / macOS

```bash
cp simulation/rivne_area/central_zone.json simulation/new_area/central_zone.json
```

Замінити:

- `id`;
- `name`;
- `description`;
- усі `tls_id`;
- назви перехресть;
- координати;
- corridors.

Файл повинен містити рівно 4–6 унікальних `tls_id`.

### 9.3 Згенерувати сфокусований трафік

#### Windows PowerShell

```powershell
python tools\generate_focused_traffic.py `
  --net simulation\new_area\osm.net.xml.gz `
  --zone simulation\new_area\central_zone.json `
  --output-dir simulation\new_area `
  --duration 1800 `
  --vehicles-per-hour 2400 `
  --routes 8
```

#### Linux / macOS

```bash
python tools/generate_focused_traffic.py \
  --net simulation/new_area/osm.net.xml.gz \
  --zone simulation/new_area/central_zone.json \
  --output-dir simulation/new_area \
  --duration 1800 \
  --vehicles-per-hour 2400 \
  --routes 8
```

Очікуване повідомлення:

```text
Generated 8 focused routes covering 4-6 connected traffic lights
```

Генератор створює:

```text
focused.rou.xml
focused.sumocfg
focused.manifest.json
```

### 9.4 Перевірити focused-сценарій

#### Windows

```powershell
sumo -c simulation\new_area\focused.sumocfg --end 120
sumo-gui -c simulation\new_area\focused.sumocfg
```

#### Linux / macOS

```bash
sumo -c simulation/new_area/focused.sumocfg --end 120
sumo-gui -c simulation/new_area/focused.sumocfg
```

### 9.5 Запустити FlowMind на новій карті

#### Windows PowerShell

```powershell
python experiments\run_flowmind.py `
  --config simulation\new_area\focused.sumocfg `
  --zone simulation\new_area\central_zone.json `
  --duration 900 `
  --gui
```

#### Linux / macOS

```bash
python experiments/run_flowmind.py \
  --config simulation/new_area/focused.sumocfg \
  --zone simulation/new_area/central_zone.json \
  --duration 900 \
  --gui
```

Інші режими:

```powershell
python experiments\run_fixed.py --config simulation\new_area\focused.sumocfg --zone simulation\new_area\central_zone.json --duration 900
python experiments\run_local_adaptive.py --config simulation\new_area\focused.sumocfg --zone simulation\new_area\central_zone.json --duration 900
```

## 10. Налаштування швидкої для нової карти

Відкрити мережу:

```powershell
netedit -s simulation\new_area\osm.net.xml.gz
```

У `netedit`:

1. знайти старт швидкої;
2. вибрати найближче passenger edge;
3. скопіювати edge ID;
4. знайти лікарню;
5. скопіювати destination edge ID;
6. перевірити напрямок обох edges.

Скопіювати конфіг:

### Windows

```powershell
Copy-Item simulation\rivne_area\emergency.json simulation\new_area\emergency.json
notepad simulation\new_area\emergency.json
```

### Linux / macOS

```bash
cp simulation/rivne_area/emergency.json simulation/new_area/emergency.json
```

Замінити:

```json
{
  "start": {
    "name": "Назва стартової точки",
    "edge_id": "START_EDGE"
  },
  "destination": {
    "name": "Назва лікарні",
    "edge_id": "HOSPITAL_EDGE"
  }
}
```

Інші поля з оригінального `emergency.json` потрібно залишити.

Перевірити маршрут у headless-режимі:

### Windows

```powershell
python experiments\run_flowmind.py `
  --config simulation\new_area\focused.sumocfg `
  --zone simulation\new_area\central_zone.json `
  --emergency `
  --emergency-config simulation\new_area\emergency.json `
  --emergency-depart 60 `
  --duration 600
```

### Linux / macOS

```bash
python experiments/run_flowmind.py \
  --config simulation/new_area/focused.sumocfg \
  --zone simulation/new_area/central_zone.json \
  --emergency \
  --emergency-config simulation/new_area/emergency.json \
  --emergency-depart 60 \
  --duration 600
```

Повна демонстрація:

### Windows

```powershell
python experiments\run_demo.py `
  --config simulation\new_area\focused.sumocfg `
  --zone simulation\new_area\central_zone.json `
  --emergency-config simulation\new_area\emergency.json `
  --duration 600
```

### Linux / macOS

```bash
python experiments/run_demo.py \
  --config simulation/new_area/focused.sumocfg \
  --zone simulation/new_area/central_zone.json \
  --emergency-config simulation/new_area/emergency.json \
  --duration 600
```

Після SUMO GUI автоматично запускається Streamlit.

## 11. Перевірка зв’язності

Якщо генератор пише:

```text
Focused routes do not cover all zone traffic lights
```

або:

```text
Selected traffic-light zone is not route-connected
```

потрібно:

1. перевірити `tls_id`;
2. прибрати ізольований світлофор;
3. вибрати ближчі перехрестя;
4. перевірити напрямки та connections у `netedit`;
5. збільшити кількість маршрутів:

```powershell
python tools\generate_focused_traffic.py `
  --net simulation\new_area\osm.net.xml.gz `
  --zone simulation\new_area\central_zone.json `
  --output-dir simulation\new_area `
  --routes 12
```

## 12. Типові проблеми

### Overpass повертає 504

Повторити завантаження:

```powershell
python "$SUMO_TOOLS\osmGet.py" `
  --bbox $BBOX `
  --prefix osm `
  --output-dir . `
  --shapes `
  --retries 5 `
  --retry-delay 10
```

Або зменшити bounding box.

### Немає світлофорів

Причини:

- в OSM не позначені traffic signals;
- область надто мала;
- `--tls.discard-simple` прибрав прості signals;
- перехрестя некоректно імпортоване.

Повторна діагностична конвертація без discard:

```powershell
netconvert `
  --osm-files osm_bbox.osm.xml `
  --output-file osm.debug.net.xml.gz `
  --geometry.remove `
  --junctions.join `
  --tls.guess-signals `
  --tls.join `
  --output.street-names
```

### Багато коротких перехресть і заторів

Переконатися, що використовується:

```text
--junctions.join
--tls.join
```

Потім перевірити junction clusters у `netedit`.

### Машини не з’являються

Перевірити route file:

```powershell
Get-Item osm.passenger.rou.xml
Select-String -Path osm.passenger.rou.xml -Pattern "<vehicle|<flow"
```

Перевірити конфіг:

```powershell
sumo -c osm.sumocfg --verbose
```

### Route errors

Повторно створити routes з `--validate`:

```powershell
python "$SUMO_TOOLS\randomTrips.py" `
  -n osm.net.xml.gz `
  -o osm.passenger.trips.xml `
  -r osm.passenger.rou.xml `
  -e 1800 `
  --insertion-rate 1200 `
  --vehicle-class passenger `
  --edge-permission passenger `
  --validate `
  --remove-loops
```

### SUMO не знаходить файли

Запускати конфіг із кореня FlowMind:

```powershell
sumo -c simulation\new_area\focused.sumocfg
```

Не переносити окремо тільки `.sumocfg`: мережа, routes і polygons повинні
залишатися поруч.

## 13. Короткий список команд

### Автоматично через Wizard

```powershell
python "$SUMO_TOOLS\osmWebWizard.py" --bbox "26.235,50.607,26.275,50.637" --output "simulation\new_area" --begin 0 --end 1800
```

### Автоматично через CLI

```powershell
New-Item -ItemType Directory -Force simulation\new_area
Set-Location simulation\new_area
python "$SUMO_TOOLS\osmGet.py" --bbox "26.235,50.607,26.275,50.637" --prefix osm --output-dir . --shapes
python "$SUMO_TOOLS\osmBuild.py" --osm-file osm.osm.xml --prefix osm --vehicle-classes passenger --typemap "$env:SUMO_HOME\data\typemap\osmPolyconvert.typ.xml" --netconvert-typemap "$env:SUMO_HOME\data\typemap\osmNetconvert.typ.xml,$env:SUMO_HOME\data\typemap\osmNetconvertUrbanDe.typ.xml" --output-directory . --gzip
python "$SUMO_TOOLS\randomTrips.py" -n osm.net.xml.gz -o osm.passenger.trips.xml -r osm.passenger.rou.xml -e 1800 --insertion-rate 2400 --vehicle-class passenger --edge-permission passenger --remove-loops --validate --seed 42
Set-Location ..\..
```

### FlowMind

```powershell
python tools\generate_focused_traffic.py --net simulation\new_area\osm.net.xml.gz --zone simulation\new_area\central_zone.json --output-dir simulation\new_area --duration 1800 --vehicles-per-hour 2400 --routes 8
python experiments\run_flowmind.py --config simulation\new_area\focused.sumocfg --zone simulation\new_area\central_zone.json --duration 900 --gui
```

### FlowMind зі швидкою

```powershell
python experiments\run_demo.py --config simulation\new_area\focused.sumocfg --zone simulation\new_area\central_zone.json --emergency-config simulation\new_area\emergency.json --duration 600
```
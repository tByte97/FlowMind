# Як підключити `simulation/new_area/map.osm` до FlowMind

У цій папці вже є сирий експорт OpenStreetMap:

```text
simulation/new_area/map.osm
```

SUMO і FlowMind не запускають `.osm` напряму. Спочатку потрібно створити
SUMO-мережу, візуальні об'єкти, опис контрольованої зони та трафік.

Інструкція нижче розрахована на Linux і поточний шлях проєкту:

```text
/home/tbyte/Документи/github/FlowMind
```

Загальна інструкція для Linux і Windows:
[docs/NEW_OSM_MAP_GUIDE.md](../../docs/NEW_OSM_MAP_GUIDE.md).

## 1. Підготувати середовище

Виконувати команди з кореня репозиторію:

```bash
cd "/home/tbyte/Документи/github/FlowMind"
source .venv/bin/activate
export SUMO_HOME="$(python -c 'import sumo; print(sumo.SUMO_HOME)')"
export PATH="$SUMO_HOME/bin:$PATH"
```

Перевірити інструменти й вхідний файл:

```bash
sumo --version
netconvert --version
polyconvert --version
ls -lh simulation/new_area/map.osm
```

## 2. Автоматичний спосіб — рекомендовано

У проєкті є скрипт, який самостійно виконує `netconvert`,
`polyconvert`, `randomTrips.py` і створює готовий `osm.sumocfg`:

```bash
python tools/build_sumo_map.py \
  simulation/new_area/map.osm \
  --output-dir simulation/new_area \
  --duration 1800 \
  --insertion-rate 1200 \
  --seed 42 \
  --force
```

`--force` у цьому прикладі потрібен, тому що в `simulation/new_area`
вже є згенерований `osm.net.xml.gz`. Для нової порожньої директорії
параметр `--force` не додавати.

Скрипт створить:

```text
osm.net.xml.gz
osm.poly.xml.gz
osm.passenger.trips.xml
osm.passenger.rou.xml
osm.sumocfg
osm.build.json
```

Перевірка:

```bash
sumo -c simulation/new_area/osm.sumocfg --end 120
sumo-gui -c simulation/new_area/osm.sumocfg
```

Корисні варіанти:

```bash
# Тільки карта без випадкового трафіку
python tools/build_sumo_map.py map.osm \
  --output-dir simulation/my_area \
  --no-traffic

# Без будівель, парків і води
python tools/build_sumo_map.py map.osm \
  --output-dir simulation/my_area \
  --no-polygons

# Інші імена вихідних файлів: rivne.net.xml.gz, rivne.sumocfg тощо
python tools/build_sumo_map.py map.osm \
  --output-dir simulation/my_area \
  --prefix rivne
```

Скрипт підтримує `.osm`, `.osm.xml`, `.osm.gz` і `.osm.xml.gz`.
Без `--force` він зупиниться, якщо вихідні файли вже існують.

`osm.build.json` зберігає SHA-256 вхідної карти, версію SUMO, параметри
генерації, кількість вузлів, доріг і світлофорів, а також список
створених файлів.

Наступні розділи показують ті самі операції вручну і потрібні для
діагностики або тонкого налаштування.

## 3. Перетворити OSM на дорожню мережу SUMO вручну

```bash
netconvert \
  --osm-files simulation/new_area/map.osm \
  --output-file simulation/new_area/osm.net.xml.gz \
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

Результат:

```text
simulation/new_area/osm.net.xml.gz
```

Для поточного `map.osm` команда перевірена на SUMO 1.27.1. Вона створює
мережу розміром приблизно `5.15 × 3.48 км` із 28 світлофорними вузлами.

## 4. Додати будівлі, парки та інші об'єкти вручну

```bash
polyconvert \
  --net-file simulation/new_area/osm.net.xml.gz \
  --osm-files simulation/new_area/map.osm \
  --type-file "$SUMO_HOME/data/typemap/osmPolyconvert.typ.xml" \
  --output-file simulation/new_area/osm.poly.xml.gz \
  --osm.keep-full-type \
  --verbose
```

Результат:

```text
simulation/new_area/osm.poly.xml.gz
```

Polygons потрібні лише для відображення карти в SUMO GUI. На маршрути й
роботу контролера вони не впливають.

## 5. Вибрати зону світлофорів

Показати всі доступні TLS ID:

```bash
python -c "import sumolib; n=sumolib.net.readNet('simulation/new_area/osm.net.xml.gz', withPrograms=True, withConnections=True); [print(t.getID(), 'links=', len(t.getConnections())) for t in n.getTrafficLights() if t.getPrograms()]"
```

Відкрити мережу для візуальної перевірки:

```bash
netedit -s simulation/new_area/osm.net.xml.gz
```

Для іншого міста потрібно вибрати 4–6 пов'язаних перехресть і вручну
створити `central_zone.json` за шаблоном
`simulation/rivne_area/central_zone.json`.

Поточний `map.osm` покриває ту саму центральну частину Рівного, і всі
шість наявних TLS ID у ньому присутні. Тому для першого запуску можна
скопіювати вже перевірену зону:

```bash
cp simulation/rivne_area/central_zone.json \
  simulation/new_area/central_zone.json
```

Важливо: не копіювати цей файл для довільної карти іншого району або
міста. TLS ID мають належати саме новій SUMO-мережі.

## 6. Згенерувати трафік через контрольовану зону

```bash
python tools/generate_focused_traffic.py \
  --net simulation/new_area/osm.net.xml.gz \
  --zone simulation/new_area/central_zone.json \
  --output-dir simulation/new_area \
  --duration 1800 \
  --vehicles-per-hour 2400 \
  --routes 8
```

Мають з'явитися:

```text
simulation/new_area/focused.rou.xml
simulation/new_area/focused.sumocfg
simulation/new_area/focused.manifest.json
```

Очікуваний результат для поточної карти:

```text
Generated 8 focused routes covering 6 connected traffic lights
```

`focused.manifest.json` показує, через які світлофори проходить кожен
маршрут. Генерація завершується помилкою, якщо зона не зв'язана або
маршрути не покривають усі вибрані світлофори.

## 7. Перевірити карту до запуску FlowMind

Спочатку виконати короткий headless-прогін:

```bash
sumo \
  -c simulation/new_area/focused.sumocfg \
  --end 120 \
  --duration-log.statistics
```

Після успішної перевірки відкрити GUI:

```bash
sumo-gui -c simulation/new_area/focused.sumocfg
```

Перевірити:

- карта і будівлі відображаються;
- автомобілі з'являються;
- маршрути проходять через центральну зону;
- світлофори перемикають фази;
- SUMO не завершується через route errors.

## 8. Запустити FlowMind на новій карті

Headless:

```bash
python experiments/run_flowmind.py \
  --config simulation/new_area/focused.sumocfg \
  --zone simulation/new_area/central_zone.json \
  --duration 600
```

Із SUMO GUI:

```bash
python experiments/run_flowmind.py \
  --config simulation/new_area/focused.sumocfg \
  --zone simulation/new_area/central_zone.json \
  --duration 600 \
  --gui
```

Для порівняння можна так само запускати `run_fixed.py` і
`run_local_adaptive.py`, передаючи ті самі `--config` та `--zone`.

## 9. Налаштувати швидку

У `simulation/new_area/emergency.json` уже задано перевірений
демонстраційний маршрут:

```text
Демо-точка виїзду біля Оксфорд Медікал
  → Міська поліклініка №2
```

Маршрут використовує валідні edge ID поточної карти та проходить через
три контрольовані світлофори.

Для власних старту й лікарні є два варіанти:

1. експортувати ширшу OSM-область, яка містить потрібну станцію швидкої,
   лікарню і весь маршрут між ними;
2. вибрати інші стартове та кінцеве ребра всередині цієї карти.

Для другого варіанта:

```bash
cp simulation/new_area/emergency.json \
  simulation/new_area/emergency.custom.json
netedit -s simulation/new_area/osm.net.xml.gz
```

У `netedit` увімкнути Edge mode, вибрати дорогу біля старту та дорогу
біля лікарні, скопіювати їхні ID і замінити в
`emergency.custom.json`:

```json
{
  "start": {
    "name": "Нова стартова точка",
    "edge_id": "START_EDGE"
  },
  "destination": {
    "name": "Нова лікарня",
    "edge_id": "HOSPITAL_EDGE"
  }
}
```

Поля `vehicle_id`, `route_id`, `vehicle_type_id`,
`base_vehicle_type_id`, `color` і параметри швидкості залишити.

Перевірити швидку без GUI:

```bash
python experiments/run_flowmind.py \
  --config simulation/new_area/focused.sumocfg \
  --zone simulation/new_area/central_zone.json \
  --emergency \
  --emergency-config simulation/new_area/emergency.json \
  --emergency-depart 60 \
  --duration 600
```

Повне демо: SUMO GUI, автоматична швидка, а після симуляції Streamlit:

```bash
python experiments/run_demo.py \
  --config simulation/new_area/focused.sumocfg \
  --zone simulation/new_area/central_zone.json \
  --emergency-config simulation/new_area/emergency.json \
  --emergency-depart 60 \
  --duration 600
```

## 10. Підсумкова структура

Після підготовки папка має виглядати так:

```text
simulation/new_area/
├── map.osm
├── osm.net.xml.gz
├── osm.poly.xml.gz
├── central_zone.json
├── focused.rou.xml
├── focused.sumocfg
├── focused.manifest.json
├── emergency.json
└── README.md
```

`emergency.json` потрібен тільки для сценарію зі швидкою.

## 11. Якщо це повністю інша карта

Для нового району або міста послідовність не змінюється:

```text
map.osm
  → netconvert
  → osm.net.xml.gz
  → polyconvert
  → osm.poly.xml.gz
  → central_zone.json із 4–6 локальними TLS ID
  → generate_focused_traffic.py
  → focused.sumocfg
  → run_flowmind.py
```

Не можна переносити зі старої карти без перевірки:

- TLS ID у `central_zone.json`;
- edge ID у `emergency.json`;
- згенеровані маршрути;
- SUMO network і polygon-файли.

Після будь-якої зміни геометрії або edge ID потрібно повторно
згенерувати `focused.rou.xml`, `focused.sumocfg` і manifest.

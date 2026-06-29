# Інструкція запуску SUMO-мапи Рівного

Ця інструкція описує, як запустити вже згенеровану мапу Рівного в SUMO після створення сценарію через `osmWebWizard`.

---

## 1. Структура згенерованої папки

Після генерації мапи через `osmWebWizard` створюється папка з датою та часом, наприклад:

```text
2026-06-29-23-11-01
```

Усередині неї знаходяться файли сценарію SUMO:

```text
osm.sumocfg
osm.net.xml.gz
osm.poly.xml.gz
osm.view.xml
osm.passenger.trips.xml
osm.netccfg
osm.polycfg
output.add.xml
stats.xml
tripinfos.xml
```

Головний файл для запуску:

```text
osm.sumocfg
```

Саме через нього відкривається вся симуляція.

---

## 2. Перехід у папку з мапою

Спочатку потрібно перейти в папку, де лежить згенерована мапа.

Приклад:

```bash
cd ~/flowmind-rivne/simulation/rivne_area
```

Або, якщо папка ще має назву з датою:

```bash
cd ~/flowmind-rivne/simulation/2026-06-29-23-11-01
```

Важливо запускати SUMO саме з цієї папки, тому що в `osm.sumocfg` використовуються відносні шляхи до інших файлів.

---

## 3. Запуск мапи в SUMO GUI

Для запуску симуляції виконай команду:

```bash
sumo-gui -c osm.sumocfg
```

Після цього має відкритися вікно `sumo-gui` з мапою.

Щоб запустити рух машин, натисни кнопку **Play** у верхній панелі або клавішу:

```text
F5
```

---

## 4. Якщо SUMO встановлений через Python venv

Якщо SUMO встановлювався через `pip install eclipse-sumo`, спочатку потрібно активувати віртуальне середовище:

```bash
source .venv/bin/activate
```

Потім виставити змінну `SUMO_HOME`:

```bash
export SUMO_HOME=$(python -c "import sumo; print(sumo.SUMO_HOME)")
```

Перевірити:

```bash
echo $SUMO_HOME
```

Після цього можна запускати мапу:

```bash
cd ~/flowmind-rivne/simulation/rivne_area
sumo-gui -c osm.sumocfg
```

---

## 5. Якщо SUMO встановлений через системний пакет

Якщо SUMO встановлений через пакетний менеджер Linux, наприклад `apt` або `dnf`, достатньо перевірити, що команди доступні:

```bash
sumo --version
sumo-gui --version
```

Після цього запуск:

```bash
cd ~/flowmind-rivne/simulation/rivne_area
sumo-gui -c osm.sumocfg
```

---

## 6. Рекомендована структура для проєкту

Щоб не тримати папки з датами, краще перенести потрібний сценарій у зрозумілу директорію:

```text
flowmind-rivne/
└── simulation/
    └── rivne_area/
        ├── osm.sumocfg
        ├── osm.net.xml.gz
        ├── osm.poly.xml.gz
        ├── osm.view.xml
        ├── osm.passenger.trips.xml
        └── output.add.xml
```

Якщо папка ще називається датою, її можна перейменувати:

```bash
mv 2026-06-29-23-11-01 rivne_area
```

Або скопіювати в проєкт:

```bash
mkdir -p ~/flowmind-rivne/simulation/rivne_area
cp -r 2026-06-29-23-11-01/* ~/flowmind-rivne/simulation/rivne_area/
```

---

## 7. Перевірка, що все працює

Перейди в папку сценарію:

```bash
cd ~/flowmind-rivne/simulation/rivne_area
```

Перевір наявність головного файлу:

```bash
ls osm.sumocfg
```

Запусти симуляцію:

```bash
sumo-gui -c osm.sumocfg
```

Якщо відкрилася мапа Рівного і після натискання **Play** рухаються машини — сценарій збережений і запускається правильно.

---

## 8. Типові проблеми

### SUMO не знаходить файли

Якщо з’являється помилка, що SUMO не може знайти `osm.net.xml.gz`, `osm.poly.xml.gz` або інший файл, найчастіше причина в тому, що запуск виконано не з тієї папки.

Правильно:

```bash
cd ~/flowmind-rivne/simulation/rivne_area
sumo-gui -c osm.sumocfg
```

Неправильно:

```bash
sumo-gui -c ~/flowmind-rivne/simulation/rivne_area/osm.sumocfg
```

У другому випадку SUMO може шукати файли відносно поточної папки, а не папки сценарію.

---

### Команда `sumo-gui` не знайдена

Перевір встановлення:

```bash
sumo --version
sumo-gui --version
```

Якщо SUMO встановлений у Python venv, активуй його:

```bash
source .venv/bin/activate
```

І вистав `SUMO_HOME`:

```bash
export SUMO_HOME=$(python -c "import sumo; print(sumo.SUMO_HOME)")
```

---

### Машини не рухаються

Перевір, що симуляцію запущено кнопкою **Play** або клавішею `F5`.

Також перевір, чи є файл із маршрутами:

```bash
ls *trips.xml
ls *rou.xml
```

У згенерованому через `osmWebWizard` сценарії зазвичай використовується файл:

```text
osm.passenger.trips.xml
```

---

## 9. Що робити далі

Після успішного запуску мапи можна переходити до наступного етапу проєкту FlowMind Rivne:

1. Вибрати конкретну транспортну область.
2. Перевірити, які перехрестя мають світлофори.
3. Запустити базову симуляцію з фіксованими світлофорами.
4. Підключити Python через TraCI.
5. Зчитувати стан транспорту та світлофорів.
6. Реалізувати перший контролер FlowMind для зонального балансування.

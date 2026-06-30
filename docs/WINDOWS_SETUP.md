# Встановлення і запуск FlowMind Rivne на Windows з нуля

Інструкція розрахована на Windows 10/11 x64 і PowerShell. Усі команди
потрібно виконувати з кореневої папки проєкту.

Рекомендовано зберігати проєкт у короткому шляху без OneDrive, наприклад:

```text
C:\Projects\FlowMind
```

## 1. Встановлення Git

Завантажити Git for Windows:

<https://git-scm.com/download/win>

Або встановити через PowerShell:

```powershell
winget install --id Git.Git -e
```

Закрити PowerShell, відкрити його знову та перевірити:

```powershell
git --version
```

## 2. Встановлення Python 3.12

Завантажити 64-бітний інсталятор Python 3.12:

<https://www.python.org/downloads/windows/>

Під час встановлення потрібно увімкнути:

```text
Add Python to PATH
Install launcher for all users
```

Перевірити встановлення в новому PowerShell:

```powershell
py -3.12 --version
```

Має відобразитися версія Python `3.12.x`.

## 3. Завантаження проєкту

Створити директорію для проєктів:

```powershell
New-Item -ItemType Directory -Force C:\Projects
Set-Location C:\Projects
```

Клонувати репозиторій:

```powershell
git clone https://github.com/tByte97/FlowMind.git
Set-Location FlowMind
```

Якщо проєкт передали ZIP-архівом, потрібно розпакувати його, відкрити
PowerShell у папці `FlowMind` і продовжити з наступного кроку.

Перевірити, що відкрито правильну директорію:

```powershell
Get-ChildItem
```

У списку мають бути `Readme.md`, `requirements.txt`, `flowmind`,
`experiments`, `simulation` і `dashboard`.

## 4. Створення віртуального середовища

У корені репозиторію виконати:

```powershell
py -3.12 -m venv .venv
```

PowerShell за замовчуванням може блокувати локальні скрипти. Дозволити їх
лише для поточного вікна:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Активувати середовище:

```powershell
.\.venv\Scripts\Activate.ps1
```

На початку рядка має з’явитися:

```text
(.venv)
```

Перевірити, що використовується Python із проєкту:

```powershell
python -c "import sys; print(sys.executable)"
```

Шлях має закінчуватися на:

```text
FlowMind\.venv\Scripts\python.exe
```

## 5. Встановлення залежностей

Оновити `pip`:

```powershell
python -m pip install --upgrade pip
```

Встановити залежності:

```powershell
python -m pip install -r requirements.txt
```

Ця команда встановлює:

- Eclipse SUMO 1.27.1;
- TraCI і sumolib;
- NumPy, Pandas і NetworkX;
- Streamlit, Plotly, Folium та інші бібліотеки.

Встановлення може тривати декілька хвилин.

## 6. Налаштування SUMO

Проєкт використовує SUMO з Python-пакета `eclipse-sumo`. Отримати шлях до
встановленого SUMO:

```powershell
$env:SUMO_HOME = python -c "import sumo; print(sumo.SUMO_HOME)"
```

Додати його виконувані файли до `PATH` поточного PowerShell:

```powershell
$env:Path = "$env:SUMO_HOME\bin;$env:Path"
```

Перевірити змінну:

```powershell
Write-Output $env:SUMO_HOME
```

Перевірити SUMO:

```powershell
sumo --version
sumo-gui --version
```

Обидві команди мають показати версію `1.27.1`.

Перевірити Python-модулі:

```powershell
python -c "import sumo, traci, sumolib; print('SUMO OK:', sumo.SUMO_HOME)"
```

## 7. Генерація сфокусованого трафіку

Згенерувати маршрути центральної зони Рівного:

```powershell
python tools\generate_focused_traffic.py
```

Очікуване повідомлення:

```text
Generated 8 focused routes covering 6 connected traffic lights
```

Команда створює або оновлює:

```text
simulation\rivne_area\focused.rou.xml
simulation\rivne_area\focused.sumocfg
simulation\rivne_area\focused.manifest.json
```

## 8. Перевірка тестів

Запустити всі тести:

```powershell
python -m unittest discover -s tests -v
```

У кінці має бути:

```text
OK
```

## 9. Швидка перевірка SUMO

Запустити 120 секунд сфокусованої симуляції без GUI:

```powershell
sumo -c simulation\rivne_area\focused.sumocfg --end 120
```

Симуляція має завершитися без помилок маршрутів.

SUMO може показати попередження про відсутні detectors для окремих
actuated-світлофорів. Це попередження з імпортованої OSM-мережі й не
блокує запуск сценарію.

## 10. Запуск одного режиму

### Fixed Mode

```powershell
python experiments\run_fixed.py --duration 900
```

### Local Adaptive Mode

```powershell
python experiments\run_local_adaptive.py --duration 900
```

### FlowMind Area Balance

```powershell
python experiments\run_flowmind.py --duration 900
```

Після завершення результати зберігаються в папці:

```text
results
```

## 11. Порівняння всіх режимів

Для швидкої перевірки:

```powershell
python experiments\run_comparison.py --duration 300
```

Для презентаційного експерименту:

```powershell
python experiments\run_comparison.py --duration 900
```

Послідовно запускаються:

```text
Fixed → Local Adaptive → FlowMind
```

Усі режими використовують однакову карту, маршрути та тривалість.

Під час першого запуску Windows Firewall може запитати дозвіл для Python
або SUMO. Достатньо дозволити доступ для приватних мереж. TraCI
використовує локальне з’єднання між Python і SUMO.

## 12. Запуск візуальної симуляції

Запустити FlowMind разом із SUMO GUI:

```powershell
python experiments\run_flowmind.py --duration 900 --gui
```

Вікно PowerShell потрібно залишити відкритим до завершення симуляції.

Щоб відкрити сфокусований сценарій без Python-контролера:

```powershell
sumo-gui -c simulation\rivne_area\focused.sumocfg
```

У SUMO GUI натиснути `F5` або кнопку Play.

## 13. Запуск дашборду

Спочатку потрібно хоча б один раз запустити порівняння режимів:

```powershell
python experiments\run_comparison.py --duration 300
```

Потім запустити Streamlit:

```powershell
streamlit run dashboard\app.py
```

У браузері відкрити:

<http://localhost:8501>

Для зупинки сервера натиснути в PowerShell:

```text
Ctrl+C
```

## 14. Повторний запуск наступного дня

Повторно встановлювати залежності не потрібно. Відкрити PowerShell і
виконати:

```powershell
Set-Location C:\Projects\FlowMind
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
$env:SUMO_HOME = python -c "import sumo; print(sumo.SUMO_HOME)"
$env:Path = "$env:SUMO_HOME\bin;$env:Path"
```

Після цього можна запускати експерименти:

```powershell
python experiments\run_flowmind.py --duration 900 --gui
```

Або дашборд:

```powershell
streamlit run dashboard\app.py
```

## 15. Оновлення проєкту

Перед оновленням перевірити локальні зміни:

```powershell
git status
```

Завантажити останні зміни:

```powershell
git pull
```

Після зміни `requirements.txt` оновити залежності:

```powershell
python -m pip install -r requirements.txt
```

Після зміни карти або зони повторно згенерувати маршрути:

```powershell
python tools\generate_focused_traffic.py
```

## 16. Типові проблеми

### `running scripts is disabled on this system`

Виконати:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### `py` або `python` не знайдено

Перевстановити Python 3.12 і ввімкнути `Add Python to PATH`. Після
встановлення закрити та повторно відкрити PowerShell.

### `No module named ...`

Переконатися, що середовище активоване:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### `sumo` або `sumo-gui` не знайдено

Виконати:

```powershell
$env:SUMO_HOME = python -c "import sumo; print(sumo.SUMO_HOME)"
$env:Path = "$env:SUMO_HOME\bin;$env:Path"
Test-Path "$env:SUMO_HOME\bin\sumo.exe"
```

Остання команда має повернути:

```text
True
```

### TraCI не може підключитися

Перевірити, що Windows Firewall не блокує `python.exe` та `sumo.exe`.
Дозволити їм локальні з’єднання в приватній мережі та повторити запуск.

Також перевірити, чи не залишився старий процес SUMO:

```powershell
Get-Process sumo* -ErrorAction SilentlyContinue
```

За потреби закрити старі вікна SUMO вручну.

### Streamlit не відкривається

Перевірити:

```powershell
streamlit --version
streamlit run dashboard\app.py
```

Потім вручну відкрити:

<http://localhost:8501>

### Встановлення залежностей пошкоджене

Видалити лише віртуальне середовище та створити його заново:

```powershell
Deactivate
Remove-Item -Recurse -Force .venv
py -3.12 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 17. Резервне встановлення SUMO через Windows Installer

Цей варіант потрібен лише тоді, коли `eclipse-sumo` з `pip` не запускає
GUI або має проблеми з DLL.

Офіційна сторінка:

<https://sumo.dlr.de/docs/Downloads.php>

Можна встановити SUMO через `winget`:

```powershell
winget install --name sumo
```

Або завантажити 64-бітний MSI installer SUMO 1.27.1.

Після MSI-інсталяції перезапустити PowerShell і перевірити:

```powershell
sumo --version
sumo-gui --version
$env:SUMO_HOME
```

MSI зазвичай сам налаштовує `PATH` і `SUMO_HOME`. Не потрібно одночасно
перемикати шляхи між різними версіями SUMO.

## 18. Мінімальний набір команд

Після встановлення весь типовий запуск виглядає так:

```powershell
Set-Location C:\Projects\FlowMind
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
$env:SUMO_HOME = python -c "import sumo; print(sumo.SUMO_HOME)"
$env:Path = "$env:SUMO_HOME\bin;$env:Path"
python tools\generate_focused_traffic.py
python experiments\run_comparison.py --duration 900
streamlit run dashboard\app.py
```

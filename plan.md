 ## Висновок аудиту

  FlowMind зараз є адаптивним контролером окремих світлофорів із локальним
  downstream-штрафом, ML-прогнозом і emergency-priority. Але він поки що не
  реалізує повноцінне зональне балансування між перехрестями.

  Знайдено 5 блокуючих проблем, які ставлять під сумнів безпеку та коректність
  порівнянь.

  ## P0 — критичні проблеми

  ### 1. Зональне керування фактично відсутнє

  У central_zone.json описані коридори Соборної та Чорновола, але loader читає
  лише список TLS і повністю ігнорує зв’язки між ними: flowmind/
  area_model.py:126, simulation/rivne_area/central_zone.json:49.

  area_pressure_by_incoming_lane():

  - обчислює локальний pressure кожної смуги;
  - записує результат за ID цієї ж смуги;
  - controller додає його назад до цієї ж смуги.

  Тобто локальний pressure просто рахується вдруге: flowmind/
  signal_policy.py:108, flowmind/signal_policy.py:192.

  Перевірка показала:

  - між шістьма TLS центральної зони немає жодного спільного lane ID;
  - зміна черги на іншому перехресті з 0 до 20 не змінює оцінки фаз першого
    перехрестя.

  Наслідок: FlowMind не бачить, що наступне перехрестя перевантажене, якщо між
  ними є проміжні SUMO edges.

  ### 2. Режим Fixed насправді є SUMO Actuated

  Мережа генерується з:

  <tls.default-type value="actuated"/>

  simulation/rivne_area/osm.netccfg:27

  У режимі fixed код лише не створює AreaSignalController: flowmind/
  experiment.py:215.

  Отже:

  - fixed — не fixed-time;
  - це вбудований адаптивний SUMO-controller;
  - порівняння «Fixed vs FlowMind» має неправильну назву;
  - FlowMind-команди накладаються на actuated-логіку SUMO, бо програма не
    переводиться під повний зовнішній контроль.

  Це робить поточний A/B baseline методологічно некоректним.

  ### 3. Safety не враховує minDur/maxDur реальної SUMO-програми

  AreaModel зберігає лише phase.duration, ігноруючи minDur та maxDur: flowmind/
  area_model.py:150.

  У поточній мережі є зелені фази з:

  - duration = 6;
  - minDur = 13;
  - maxDur = 50.

  А effective_min_green() може дозволити вихід уже після 6 секунд: flowmind/
  signal_policy.py:79.

  Додатково controller повністю пропускає yellow/all-red фази: flowmind/
  controller.py:113. Тому clearance_seconds=5 із validator не контролює
  автоматичний перехід SUMO. У мережі є yellow-фази тривалістю 3 секунди.

  Наслідок: safety-конфігурація не гарантує заявлені таймінги.

  ### 4. Emergency-priority може відкрити рух у заблокований downstream

  Заблокований вихід дає лише штраф -30: flowmind/signal_policy.py:47.

  Emergency link отримує +1000: flowmind/signal_policy.py:211.

  Тобто emergency-priority безумовно перекриває downstream-захист. Якщо після
  перехрестя немає місця, система все одно може активувати напрямок і
  заблокувати junction box.

  SafetyValidator перевіряє порядок фаз, але не перевіряє фізичне місце після
  перехрестя.

  ### 5. Метрики не дають надійного доказу покращення

  flowmind/metrics.py:82 має декілька методологічних проблем:

  - throughput — усі прибуття у всій SUMO-мережі, а не пропускна здатність
    контрольованої зони;

  - average_travel_time враховує лише автомобілі, які встигли завершити маршрут;
  - автомобілі, що залишилися в заторі наприкінці, виключаються;
  - якщо ніхто не прибув, travel time стає 0, що виглядає як ідеальний
    результат;

  - waiting time — середнє від середніх snapshot-значень, а не середнє на
    завершену поїздку;

  - gridlock_risk — частка outgoing-смуг вище порога, а не реальний gridlock;
  - summary не містить seed, scenario hash, traffic demand, версію мережі та
    версію controller.

  Архів має лише 8 парних fixed–FlowMind запусків. У них waiting/queue трохи
  кращі, але throughput і stops гірші; до того ж «fixed» є actuated. Поточні
  цифри не можна вважати валідованим доказом ефективності.

  ## P1 — високий пріоритет

  ### ML-прогноз використовується як неперевірений counterfactual

  Dataset записує реальні current_phase, phase_state і phase_elapsed: flowmind/
  ml_dataset.py:132.

  Runtime для кожної кандидатної фази підміняє:

  - current_phase на індекс кандидата;
  - phase_state на стан кандидата;
  - phase_elapsed на 0 для неактивної фази.

  flowmind/queue_forecast.py:104, flowmind/queue_forecast.py:193

  Це counterfactual, на якому модель прямо не навчалась.

  Додаткові ризики:

  - dataset зібраний на simulation/new_area із 2400 авто/год;
  - demo використовує rivne_area із 3600 авто/год;
  - emergency розширює зону з 6 до приблизно 10 TLS, частина яких не була у
    train;

  - lane/movement hashes подаються в tree model як звичайні числа з
    беззмістовним порядком;

  - «0 ML failures» означає лише відсутність exception, а не правильність
    прогнозу;

  - exception прогнозу повністю приховується: flowmind/controller.py:154.

  ### Sensor failure працює fail-open

  Усі TraCI-помилки перетворюються на нулі: flowmind/traffic_state.py:177.

  Для controller це може означати:

  - queue = 0;
  - occupancy = 0;
  - downstream виглядає вільним;
  - несправна смуга отримує дозвіл на рух.

  Немає:

  - стану unknown;
  - timestamp останнього коректного вимірювання;
  - TTL;
  - last-known-good;
  - переходу на безпечний fixed plan.

  Fallback також змішує sensor-window дані з aggregate-метриками всієї смуги.

  ### ControlConfig не валідований

  ControlConfig не має __post_init__: flowmind/config.py:18.

  Код приймає:

  ControlConfig(
      decision_interval=0,
      min_green=50,
      max_green=10,
      blocked_occupancy=1.5,
  )

  Це може спричинити division/modulo by zero або логічно неможливі режими.

  ### Нестабільний scheduler

  Controller запускається через:

  if int(simulation_time) % decision_interval:
      return

  flowmind/controller.py:87

  При fractional step 3.1, 3.5, 3.9 рішення буде виконане кілька разів у межах
  одного інтервалу. Така сама проблема є в MetricsCollector і
  MLDatasetCollector.

  ### Green corridor фактично керує одним TLS

  flowmind/corridor_manager.py:156 повертає override лише для одного найближчого
  світлофора.

  Також:

  - PREPARE і GREEN_WINDOW мають однакову управляючу дію;
  - RECOVERY лише чекає таймер і не відновлює phase offsets;
  - TLS позначається завершеним, щойно getNextTLS() починає повертати інший;
  - напис «плавно повертає стандартний порядок фаз» не відповідає фактичній
    логіці.

  ### Відсутній інтеграційний тест controller

  38 релевантних unit-тестів проходять, але в tests/ немає жодного тесту
  AreaSignalController.step().

  Не перевіряються:

  - фактичні setPhase() і setPhaseDuration();
  - actuated-взаємодія;
  - remote congestion;
  - max-green;
  - priority timeout;
  - sensor failure;
  - одночасні рішення кількох TLS.

  ## P2 — інші недоліки

  - program = next(iter(programs.values())) довільно бере першу TLS-програму і
    не звіряє її з активною програмою SUMO: flowmind/area_model.py:145.

  - Scoring рахується по connections, тому одна incoming lane з кількома turns
    має більшу статистичну вагу.

  - Best phase не вибирається прямо: controller лише продовжує поточну або
    переходить до наступної фази циклу.

  - Pedestrian demand не входить у TrafficState.
  - Відсутня окрема оцінка junction-box occupancy.
  - Telemetry і запис JSON виконуються синхронно в simulation loop.
  - Повільний WebSocket-клієнт може затримувати publish().
  - Помилка запуску telemetry-сервера зупиняє весь experiment.
  - Моделі заявляють 300 train-файлів, але зараз у workspace доступні лише 90 —
    повне відтворення artifacts неможливе.

  - Scenario називається rivne_focused, хоча dataset plan використовує
    simulation/new_area: results/dataset/dataset_plan.json:4.

  ## Чекліст виправлення

  ### Етап 1 — правильна база і safety

  - [x] Створити справжню static fixed-time TLS-програму для baseline.
  - [x] Явно розділити режими static_fixed, sumo_actuated, local, flowmind.
  - [x] На старті логувати active SUMO program ID і type для кожного TLS.
  - [x] Додати в Intersection program_id, program_type, minDur, maxDur.
  - [x] Заборонити transition раніше SUMO minDur.
  - [x] При вході в yellow/all-red застосовувати реальні SUMO
    duration/minDur; clearance_seconds використовувати лише як fallback.
  - [x] Додати demand-aware hard gate для blocked downstream з
    мінімальним запасом free storage slots.
  - [x] Для emergency вимагати окремий гарантований storage buffer,
    навіть якщо авто ще не потрапило в sensor window.

  - [x] Додати незалежну від SUMO конфліктну матрицю рухів,
    startup-validation всіх TLS plans і fail-fast JSON-аудит.

  ### Етап 2 — справжній зональний граф

  - [x] Завантажувати corridors із central_zone.json.
  - [x] Побудувати directed graph TLS → road segments → TLS.
  - [x] Зв’язати outgoing lane одного вузла з downstream incoming lane іншого
    через проміжні edges.

  - [x] Передавати downstream pressure на 1–2 наступні перехрестя.
  - [x] Ввести node storage capacity та spillback probability.
  - [x] Робити один area decision snapshot, а не незалежні локальні рішення.
  - [x] Додати тест: затор на I-03 повинен змінювати рішення I-02.
  - [x] Додати тест: віддалений, не пов’заний TLS не повинен впливати на I-02.

  ### Етап 3 — відмовостійкість

  - [x] Додати повну валідацію ControlConfig.
  - [x] Замінити modulo scheduler на next_decision_at.
  - [x] Ввести LaneState.valid, sample_time, error.
  - [x] Використовувати last-known-good із коротким TTL.
  - [x] При втраті даних переходити на перевірений fallback plan.
  - [x] Логувати причину кожного safety rejection.
  - [x] Окремо рахувати sensor failures, stale lanes і fallback activations.
  - [x] Не дозволяти telemetry/dashboard failure зупиняти control loop.

  ### Етап 4 — ML

  - [x] Визначити, модель є forecast current-policy чи counterfactual phase
    model.

  - [x] Не подавати candidate phase як фактичний current_phase без відповідного
    train dataset.

  - [x] Прибрати raw numeric hashes або перейти на контрольовані categorical
    buckets.

  - [x] Додати prediction bounds відповідно до lane capacity.
  - [x] Додати confidence/OOD detector.
  - [x] Для unseen TLS/lane автоматично вимикати ML-вплив.
  - [x] Перенавчити модель на тому самому map/demand, що й demo.
  - [x] Окремо перевірити 3600 авто/год і emergency-expanded zone.
  - [x] Спочатку запускати ML у shadow mode і порівнювати з фактичними чергами.
  - [x] Версіонувати dataset hash, network hash, feature schema і model
    artifact.

  ### Етап 5 — emergency corridor

  - [x] Розділити дії PREPARE та GREEN_WINDOW.
  - [x] Готувати декілька наступних TLS, а не лише один.
  - [x] Не позначати TLS завершеним до підтвердженого факту проїзду.
  - [x] Додати hard downstream safety для швидкої.
  - [x] Реалізувати справжній recovery phase/offset plan.
  - [x] Додати динамічне rerouting або повторну оцінку маршруту перед departure.
  - [x] Вимірювати вплив пріоритету на цивільний транспорт, а не оцінювати його
    евристикою.

  ### Етап 6 — чесна оцінка

  - [x] Додати у summary seed, scenario, demand, network hash, controller/model
    version.

  - [x] Рахувати zone inflow/outflow через boundary edges.
  - [x] Додати completed і unfinished/censored travel times.
  - [x] Не повертати 0 для відсутньої метрики — використовувати null.
  - [x] Перейменувати поточний gridlock_risk на blocked_outgoing_share.
  - [ ] Виконати щонайменше 30–50 повних пар для кожного режиму.
    Локально збережено 6/30 checkpoint-пар; за рішенням користувача локальний
    прогін зупинено, а повний запуск перенесено на сервер командою
    `experiments/run_evaluation.py ... --resume`.
  - [x] Порівнювати однакові seed/config/demand/emergency route.
  - [x] Додати confidence intervals і paired statistical tests.
  - [x] Ввести regression gates: waiting, throughput, stops, spillback та
    emergency ETA.

  Рекомендований порядок: спочатку fixed baseline і safety timing, потім
  реальний зональний graph, після цього integration tests, і лише тоді ML та
  повторне A/B-тестування. До завершення перших трьох етапів систему краще
  описувати як «адаптивний multi-intersection prototype», а не як валідоване
  зональне керування.

## Актуалізація P0–P3 для 20 TLS (2026-07-13)

Цей розділ є актуальним станом після переходу від старого 6-TLS
dataset/current-policy ML до Rivne 20-TLS decision model.

### P0 — узгодженість

- [x] `rivne_area/focused.sumocfg` і `central_zone.json` є явними defaults
  `run_dataset.py` та аргументами репозиторного `compose.yaml`.
- [x] Versioned Docker build з OCI/image commit та strict startup contract.
- [x] Startup audit перевіряє commit, TLS count, network/zone/dataset
  schema hashes, model artifact/feature schema і coverage TLS/lanes; emergency-expanded
  area перевіряється повторно.
- [x] Dataset fingerprint включає scenario/network/zone/schema/controller
  source/demand/run plan; `--resume` відхиляє legacy і mismatch.
- [x] 6-TLS моделі заархівовані у `models/archive/zone6` і виключені
  з Docker build context.
- [x] `model-init` автоматично виставляє UID/GID 10001 для
  `flowmind_models`.
- [ ] Зібрати новий 20-TLS dataset і три decision-моделі. Це довгий
  compute-job, який має виконуватися вже на новому image. Dataset,
  що зараз виконується на сервері, придатний лише як baseline/archive:
  він не має нової schema/action/history/demand diversity і не може навчати
  фінальну decision model.

### P1 — зональна ефективність

- [x] Горизонт 30–90 с для platoon arrivals, storage і downstream occupancy.
- [x] Спільний `zone_optimizer` вибирає фази всіх TLS coordinate-descent
  над `delay + queue growth + spillback + stops - throughput` з парними
  corridor/platoon terms.
- [x] Phase capacity враховує saturation flow і turning ratio, а не середній
  score дубльованих connections.
- [x] Sensor fallback активується лише для проблемного TLS;
  graph segment із invalid data залишається fail-closed.
- [x] Segment/node capacity калібрується в тій самій sensor window
  (за замовчуванням 120 м).
- [ ] Динамічне маскування окремої blocked movement всередині
  спільної SUMO-фази не увімкнено: поточно вся demanded-фаза
  fail-closed. Безпечне виправлення потребує startup-генерації та
  conflict-validation маскованих TLS plans; простий
  `setRedYellowGreenState` зруйнує перевірену послідовність clearance і не є
  прийнятним shortcut.

### P2 — ML decision model

- [x] Dataset schema v3 містить observed action/candidate phase, history 15/30 с,
  arrival/discharge, downstream storage/occupancy, сусідні TLS і platoon.
- [x] Targets: delta queue, queue reduction, future waiting і discharged vehicles;
  30/60/90 ensemble за замовчуванням навчається на queue reduction.
- [x] Demand рандомізує off-peak/normal/morning/evening/oversaturated,
  incident/lane closure та emergency, а не лише seed однакового OD.
- [x] Runtime не підміняє actual current phase candidate-фазою; negative
  queue reduction зберігається в physical bounds.
- [x] Shadow MAE порівнює target лише для фактично виконаної фази,
  а не для контрфактичних alternatives.

### P3 — validation і tuning

- [x] Shadow/control evaluation має окремі resumable IDs і regression gates.
- [x] `approve_queue_control.py` вимагає pass обох evaluation, MAE/OOD bounds і
  пише approval, прив'язаний до models/network/zone/ControlConfig hashes.
- [x] Web залишає ML у shadow, доки немає matching approval і
  `FLOWMIND_ENABLE_QUEUE_CONTROL=1`.
- [x] `tune_control.py` реалізує resumable Optuna/TPE Bayesian search;
  його `best_control_config.json` можна передати demo/evaluation.
- [ ] Фактично виконати tuning, 30 shadow pairs, 30 ML-control pairs і approval
  після завершення нового 20-TLS training. До цього фінальну
  evaluation не запускати.

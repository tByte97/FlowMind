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
  - [x] Узгодити clearance_seconds із реальними yellow/all-red фазами.
  - [ ] Додати hard gate для blocked downstream.
  - [ ] Для emergency дозволяти рух лише за наявності гарантованого storage
    space.

  - [ ] Додати конфліктну матрицю рухів та startup-validation усіх TLS plans.

  ### Етап 2 — справжній зональний граф

  - [ ] Завантажувати corridors із central_zone.json.
  - [ ] Побудувати directed graph TLS → road segments → TLS.
  - [ ] Зв’язати outgoing lane одного вузла з downstream incoming lane іншого
    через проміжні edges.

  - [ ] Передавати downstream pressure на 1–2 наступні перехрестя.
  - [ ] Ввести node storage capacity та spillback probability.
  - [ ] Робити один area decision snapshot, а не незалежні локальні рішення.
  - [ ] Додати тест: затор на I-03 повинен змінювати рішення I-02.
  - [ ] Додати тест: віддалений, не пов’язаний TLS не повинен впливати на I-02.

  ### Етап 3 — відмовостійкість

  - [ ] Додати повну валідацію ControlConfig.
  - [ ] Замінити modulo scheduler на next_decision_at.
  - [ ] Ввести LaneState.valid, sample_time, error.
  - [ ] Використовувати last-known-good із коротким TTL.
  - [ ] При втраті даних переходити на перевірений fallback plan.
  - [ ] Логувати причину кожного safety rejection.
  - [ ] Окремо рахувати sensor failures, stale lanes і fallback activations.
  - [ ] Не дозволяти telemetry/dashboard failure зупиняти control loop.

  ### Етап 4 — ML

  - [ ] Визначити, модель є forecast current-policy чи counterfactual phase
    model.

  - [ ] Не подавати candidate phase як фактичний current_phase без відповідного
    train dataset.

  - [ ] Прибрати raw numeric hashes або перейти на контрольовані categorical
    buckets.

  - [ ] Додати prediction bounds відповідно до lane capacity.
  - [ ] Додати confidence/OOD detector.
  - [ ] Для unseen TLS/lane автоматично вимикати ML-вплив.
  - [ ] Перенавчити модель на тому самому map/demand, що й demo.
  - [ ] Окремо перевірити 3600 авто/год і emergency-expanded zone.
  - [ ] Спочатку запустити ML у shadow mode і порівняти з фактичними чергами.
  - [ ] Версіонувати dataset hash, network hash, feature schema і model
    artifact.

  ### Етап 5 — emergency corridor

  - [ ] Розділити дії PREPARE та GREEN_WINDOW.
  - [ ] Готувати декілька наступних TLS, а не лише один.
  - [ ] Не позначати TLS завершеним до підтвердженого факту проїзду.
  - [ ] Додати hard downstream safety для швидкої.
  - [ ] Реалізувати справжній recovery phase/offset plan.
  - [ ] Додати динамічне rerouting або повторну оцінку маршруту перед departure.
  - [ ] Вимірювати вплив пріоритету на цивільний транспорт, а не оцінювати його
    евристикою.

  ### Етап 6 — чесна оцінка

  - [ ] Додати у summary seed, scenario, demand, network hash, controller/model
    version.

  - [ ] Рахувати zone inflow/outflow через boundary edges.
  - [ ] Додати completed і unfinished/censored travel times.
  - [ ] Не повертати 0 для відсутньої метрики — використовувати null.
  - [ ] Перейменувати поточний gridlock_risk на blocked_outgoing_share.
  - [ ] Виконати щонайменше 30–50 повних пар для кожного режиму.
  - [ ] Порівнювати однакові seed/config/demand/emergency route.
  - [ ] Додати confidence intervals і paired statistical tests.
  - [ ] Ввести regression gates: waiting, throughput, stops, spillback та
    emergency ETA.

  Рекомендований порядок: спочатку fixed baseline і safety timing, потім
  реальний зональний graph, після цього integration tests, і лише тоді ML та
  повторне A/B-тестування. До завершення перших трьох етапів систему краще
  описувати як «адаптивний multi-intersection prototype», а не як валідоване
  зональне керування.

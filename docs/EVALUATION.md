# Відтворювана оцінка FlowMind

Цей документ описує доказовий benchmark, а не демонстраційний одиночний
запуск. Реалізація знаходиться в `experiments/run_evaluation.py`, статистика —
у `flowmind/evaluation.py`.

## Що є валідною парою

Одна replicate-пара містить рівно по одному запуску кожного режиму:

- `static_fixed`;
- `sumo_actuated`;
- `local`;
- `flowmind`.

Пара відхиляється цілком, якщо відсутній або дублюється режим чи між режимами
відрізняється хоча б один інваріант:

- seed;
- scenario, network, zone і demand/route-file SHA-256;
- суттєвий RunConfig SHA-256;
- declared traffic demand і duration;
- фактичний emergency-route SHA-256.

Шлях results, WebSocket port, GUI та назва режиму не входять у paired config
hash. Усі control-параметри, duration, zone, модельні paths і emergency config
входять. Маршрут швидкої спочатку обирається один раз, після чого той самий
список SUMO edges передається решті режимів. SUMO тут залишається лише data і
simulation adapter; статистичний контракт працює зі звичайними summary JSON.

## Вибірка

Повний профіль приймає від 30 до 50 валідних пар. Прапор `--allow-small`
призначений лише для smoke-тесту інтеграції та позначає report як `smoke`.
Такий report не можна використовувати для презентаційної заяви про
ефективність.

Типовий запуск:

```bash
python experiments/run_evaluation.py \
  --replicates 30 \
  --duration 1800 \
  --workers 4 \
  --evaluation-id rivne_full_v1
```

Resume не змішує різні експерименти: manifest перевіряє modes, seeds,
duration, scenario, emergency config, sensor range і model paths до повторного
використання summary.

## Метрики і `null`

- `zone_outflow` — авто, які перетнули вихідні boundary edges фіксованої
  evaluation-зони; `throughput` є сумісним alias цієї величини.
- `average_waiting_time` — середнє snapshot waiting у контрольованій зоні.
- `stops_count` — входження авто у stopped state в зоні.
- `blocked_outgoing_share` — частка валідно виміряних outgoing lanes вище
  blocking threshold.
- `emergency_eta` — фактичний час від departure до arrival швидкої.
- `completed_travel_time_mean` охоплює завершені поїздки.
- `unfinished_travel_time_lower_bound_mean` та `*_trip_outcomes.csv` явно
  зберігають censored поїздки, які не завершились до кінця observation window.

Відсутнє значення залишається `null`. Воно виключається з числової статистики,
але зменшує paired sample count; gate переходить у `insufficient_data`, якщо
залишається менше необхідної кількості пар. `null` ніколи не стає нулем.

## Статистика

Для FlowMind проти кожного baseline окремо звіт містить:

- baseline і contender mean;
- paired difference `FlowMind − baseline`;
- 95% Student-t confidence interval для paired differences;
- двосторонній paired t-test;
- Cohen's dz;
- кількість використаних і пропущених пар.

Статистична значущість не підміняє safety/non-regression criterion. Gates
перевіряють верхню 95% межу погіршення:

| Gate | Допустима верхня межа погіршення |
| --- | --- |
| average waiting time | +5% |
| zone outflow | −2% |
| stops count | +5% |
| blocked outgoing share | +0.02 absolute |
| emergency ETA | +10% |

Загальний статус `pass` можливий лише за 30–50 валідних emergency-пар і
проходження всіх gates проти кожного baseline. Інші статуси (`regression`,
`insufficient_data`, `invalid_pairs`, `partial_missing_emergency`) є
результатом перевірки, а не помилкою генерації report; негативні результати не
приховуються.

## Артефакти

У `results/evaluation/<evaluation-id>/` створюються:

- `evaluation_manifest.json` — параметри, progress і фінальний статус;
- `summaries.json` — усі raw summaries;
- `evaluation_report.json` — машинозчитуваний висновок;
- `paired_metrics.csv` — paired statistics;
- `regression_gates.csv` — окремі gates;
- `validated_pairs.csv` — seed/config/route identity кожної прийнятої пари;
- `pair_NNN_seed_N/<mode>/` — timeseries, trip outcomes, startup safety audits
  та SUMO raw outputs конкретного запуску.

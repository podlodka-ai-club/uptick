# Memory tower: результаты поэтапной проверки

**Результат:** сквозная механика памяти проверена на реальном файловом опыте,
семь файловых arms и шесть свежих SRE-попыток выполнены. Дополнительная польза
высших слоёв не подтверждена; вклад описательных цепочек в SRE не установлен.
Рабочим остаётся эпизодический профиль. Завершающее ревью отчёта отдельно
фиксирует границу этих выводов.

## Что доказано

На реальной локальной файловой задаче подтверждён сквозной путь
`наблюдение → эпизод → кандидат → валидация → сохранённое знание → scoped cold
retrieval → сериализованный запрос решения → пересмотр после контрпримеров`.
Контрпримеры переводят устаревшие lesson/world/tool записи в disputed и не дают
им пройти холодное извлечение; уточнённые условия снова извлекаются только в
подходящем scope. Это доказательство механики обучения и пересмотра на
`file-task`, а не доказательство переноса на SRE.

На SRE пока нет проверенной immutable identity для свежего опыта. Operation-chain
артефакты остаются наблюдаемыми описательными связями: фактическая передача в
запрос подтверждена, но причинный закон и полезность ещё не подтверждены.
Развёртывание chain-знаний в рабочий профиль преждевременно.

## Зафиксированный рабочий baseline и воспроизводимость

Sealed-планы ссылаются на точный raw-конфиг:
`inputs/raw-memory-configuration.json` (локальный файл: `artifacts/memory-tower-preparation/chain-launch-preparation-01/inputs/raw-memory-configuration.json`),
абсолютный путь `/Users/mingazhev/Repos/podlodka/uptick/vadim/artifacts/memory-tower-preparation/chain-launch-preparation-01/inputs/raw-memory-configuration.json`.
SHA-256 самого файла: `92b51d3e1c370956849dd8bb3fbc53aceec6be57cab7269035eb9ce900537aad`.
Sealed-планы также фиксируют его configuration fingerprint
`2a8dcc9d9fb4f6e2118aa135fe8131b92755558fe5d95965790344abb8e9d4a2`;
профиль — `episodic-1.1-8k-advanced`.

В этом baseline включены episodic v1.2, raw recall и advanced retrieval (до трёх
элементов, не более одного на run); legacy compatibility, lessons, world,
tool knowledge, playbooks, consolidation, forgetting, semantic и structured
retrieval выключены. Это experimental working baseline, а не новый live default.

Воспроизводимость обеспечивается чтением sealed config/plan/protocol, frozen
корпусом, manifest- и source-хэшами, fingerprint конфигурации в readout и
раздельными изолированными выходами arm-ов. Raw memory config содержит политику
redact-or-reject для секретов; токены, переменные окружения и credentials в этот
отчёт не входят. Новые команды или незафиксированные параметры не добавлялись.
Основные ссылки: `authorization-and-launch.json` (локальный файл: `artifacts/memory-tower-preparation/chain-launch-preparation-01/authorization-and-launch.json`),
`launch-seal.json` (локальный файл: `artifacts/memory-tower-preparation/chain-launch-preparation-01/launch-seal.json`).

## Решение по механизмам для рабочего профиля

| Механизм | Решение | Основание и ограничение |
|---|---|---|
| Working memory | Оставить ограниченную; исправить детализацию | Ограниченная history сохраняет summary, но реальный prefix может скрыть важную роль backend. Offline structural prototype сохранил 16/69 leaves и всё ещё пропустил backend; вердикт (локальный файл: `artifacts/memory-tower-preparation/observation-structural-prototype-01/VERDICT.md`) — не deploy. |
| Episodic memory | Оставить baseline | Реальные переходы, provenance, raw outcome и bounded retrieval работают; полезность высших слоёв относительно этого baseline не показана. |
| File handoff | Оставить только opt-in | Stage08 compact summaries дали QA 4/4 против history 3/4 без charged reads и с −1.43% provider tokens. Отдельный Stage09 charged reader дал detail 2/2 против baseline 0/2, но 2.06× токенов; additive profile увеличивает collection bytes примерно на 69%. |
| Lessons | Исключить из working default, сохранить experimental | На file-task lesson прошёл strict activation, counterexample и scoped cold retrieval; Stage24 не дал прироста решения. |
| World knowledge | Исключить из working default, сохранить experimental | Локальные strict patterns и revision работают, но перенос на SRE невозможен без immutable identity; utility не измерена. |
| Tool knowledge | Исключить из working default, сохранить experimental | Local-file identity и strict validation подтверждены; changed-always claim disputed, SRE identity и equal-budget utility отсутствуют. |
| Playbooks | Исключить из working default, сохранить experimental | Inspect→deduplicate sequence наблюдалась, noop sequence disputed; independent decision utility не показана. |
| Operation-chain index | Исключить из working default, сохранить как observation overlay | 146 наблюдаемых chains и live provider exposure доказывают транспорт и provenance, но не utility или causal validity. |
| Existing advanced lexical/diversity retrieval | Оставить в текущем baseline | Sealed raw profile уже включает advanced retrieval, lexical weighting, deduplication и diversity cap; отдельного higher-layer utility это не доказывает. |
| Semantic / structured / reasoned retrieval | Исключить из default до held-out проверки | Development fixture показывает retrieval-различия, но не реальную пользу решения при равном бюджете и latency. |
| Consolidation | Исключить из default; оставить явный opt-in | Dry/apply/reapply/cold и сохранность source проверены; это не compression и decision utility не измерена. |
| Compression / summaries | Исключить из savings-профиля; prototype исправлять отдельно | Manifest-only summaries дали 619 операций, 0 materialized summaries и +1.31 MB payload без изменения retrieval. Lossless dictionary prototype: −19.88% на одном direct context, но +1.42% на меньшем; runtime/model не проверены. |
| Forgetting / physical deletion | Физическое удаление оставить выключенным | Age decay обратим. На копии удалён только disposable orphan с tombstone/idempotency; real transition был защищён retention/snapshot. |
| xMemory adapter | Исключить из оценки по умолчанию | Есть schema/smoke, но нет immutable export/read-only evidence полного pipeline. |

## Измеренные результаты

### Реальная file-task проверка

Stage24 выполнил все семь arms (none, episodic, lessons, world, tools,
playbooks, full). Каждый сделал одну и ту же `deduplicate` action и завершился
с `duplicate_lines=0`. Episodes: 17 897 provider tokens; full: 23 505,
то есть **+31.33%** при том же результате. Всего 134 607 tokens, 7 requests,
0 retries; денежная стоимость недоступна. Это отрицательный результат
incremental utility для одной простой прозрачной задачи, не обобщение на все
задачи. Данные: `analysis.json` (локальный файл: `artifacts/memory-tower-preparation/file-evaluation-model-01/analysis.json`),
`report.json` (локальный файл: `artifacts/memory-tower-preparation/file-evaluation-model-01/report.json`),
описание протокола — `STAGE24.md` (локальный файл: `artifacts/memory-tower-preparation/STAGE24.md`).

Граница версии: Stage24 использует frozen Stage11 snapshot с broad
lesson/world/tool записями до последующего revision в disputed. Поэтому он не
измеряет utility текущего refined/nested профиля; для последнего подтверждены
cold retrieval и исключение опровергнутых записей, но не model decision utility.
`completion-learning-audit.md` (локальный файл: `artifacts/memory-tower-preparation/completion-learning-audit.md`).

### Свежая SRE-серия: вклад цепочек не установлен

Разрешены шесть Terra-medium попыток, seeds 101–103, максимум два параллельно.
По authoritative `authorization-and-launch.json` (локальный файл: `artifacts/memory-tower-preparation/chain-launch-preparation-01/authorization-and-launch.json`)
все шесть попыток уже запущены и завершили terminal state; replacements не
разрешены. `chain-series-readout.json` (локальный файл: `artifacts/memory-tower-preparation/chain-launch-preparation-01/chain-series-readout.json`)
фиксирует 6/6 terminal и 1 полный горизонт.

| Seed / arm | Исход | Часы | Решения completed/requested | Действия | Uptime | Расходы среды, ₽ | Учтённые токены | Chain requests |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 101 control | completed, 100 баллов | 48.000 | 255/255 | 278 | 99.9167% | 22,873,684.51 | 8,229,922 | 0 |
| 101 treatment | decision timeout | 42.204 | 390/391 | 419 | 99.5259% | 18,504,329.61 | 12,631,764 | 8 |
| 102 control | decision timeout | 37.915 | 301/302 | 326 | 99.9516% | 17,990,265.02 | 9,810,274 | 0 |
| 102 treatment | decision timeout | 47.503 | 243/244 | 273 | 99.9845% | 22,616,678.19 | 7,892,952 | 6 |
| 103 control | decision timeout | 9.000 | 27/28 | 36 | 100.0000% | 2,313,060.48 | 871,572 | 0 |
| 103 treatment | decision timeout | 10.571 | 210/211 | 226 | 99.8423% | 3,084,071.04 | 6,793,029 | 2 |

Расходы и uptime у timeout-строк относятся только к наблюдённому префиксу.

У 101 treatment reported tokens на 53.49% выше control; частичная стоимость
среды не является экономией и не заменяет полный matched outcome. Все пять
неуспешных runs завершились на общей для harness границе: одна decision
превысила лимит 120 секунд, после чего frozen harness завершил run. Это общая
reliability limitation; её нельзя причинно приписывать включению chain-памяти.
Для timeout runs финальное потребление отменённого запроса недоступно, поэтому
reported tokens — нижняя граница. Полная серия дала 1/3 завершённых control,
0/3 завершённых treatment и 16 фактических chain requests; matched-pair
экономику и causal utility она не устанавливает. Для 103 treatment UTC elapsed
составил примерно 100 минут, с большим расхождением UTC и monotonic elapsed; причина расхождения неизвестна; terminal
сработал на decision 211 (120.1219 s), а не на общем 7200-second wall limit.
Для чисел выше использованы authoritative launch record, series readout и terminal
summaries: `control-101` (локальный файл: `artifacts/memory-tower-preparation/chain-launch-preparation-01/control-101-terminal-summary.json`),
`treatment-101` (локальный файл: `artifacts/memory-tower-preparation/chain-launch-preparation-01/treatment-101-terminal-summary.json`),
`treatment-102` (локальный файл: `artifacts/memory-tower-preparation/chain-launch-preparation-01/treatment-102-terminal-summary.json`),
`control-102` (локальный файл: `artifacts/memory-tower-preparation/chain-launch-preparation-01/control-102-terminal-summary.json`),
`control-103` (локальный файл: `artifacts/memory-tower-preparation/chain-launch-preparation-01/control-103-terminal-summary.json`),
`treatment-103` (локальный файл: `artifacts/memory-tower-preparation/chain-launch-preparation-01/treatment-103-terminal-summary.json`).

Для 103 treatment процесс прогона `27677` отсутствует после exit; root-проверка
сохранила 28 sealed hashes без изменений, а historical corpus остался unchanged.

Счётчики provider attempts в `chain-series-readout.json` (локальный файл: `artifacts/memory-tower-preparation/chain-launch-preparation-01/chain-series-readout.json`)
относятся только к structured_result и не включают все попытки отменённого
запроса; terminal public evaluation отделён от периодического overview.

В шести terminal arms медиана успешного provider request составляет 10.7–12.2 s,
P95 — 14.5–24.7 s, максимум — 15.1–83.4 s (nearest-rank). Это structured_result
telemetry только по успешным запросам: cancelled/failed requests исключены,
поэтому percentiles не описывают весь request stream. Политика retry/timeout
во время sealed-серии не менялась.

Воспроизведение readout из `vadim/`:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 artifacts/memory-tower-preparation/chain-launch-preparation-01/summarize_chain_series.py
```

Генератор (локальный файл: `artifacts/memory-tower-preparation/chain-launch-preparation-01/summarize_chain_series.py`)
не запускает модель или симулятор. Проверка сводки (локальный файл: `artifacts/memory-tower-preparation/chain-launch-preparation-01/series-readout-verification.json`)
сопоставила все terminal evaluations, source integrity, JSONL parsing и арифметику
токенов: 46,229,513 учтённых токенов; цена LLM неизвестна. Это проверка учёта,
а не доказательство полезности памяти.

## Итог и границы применения

Все запланированные попытки завершены и включены в анализ. Пять таймаутов
не дают выделить вклад chain-памяти в результат; новых попыток в этом
разрешении нет. Нельзя ни заявить экономический эффект, ни причинно обвинить
память в незавершении. Проверенный рабочий профиль указан выше; experimental
слои не включаются в него по результатам одних механических проверок.

Аудит требований Goal (локальный файл: `artifacts/memory-tower-preparation/GOAL_COMPLETION_AUDIT.md`)
сопоставляет исходный план с фактическими артефактами. Ограниченные evidence
аудиты (learning (локальный файл: `artifacts/memory-tower-preparation/completion-learning-audit.md`),
memory ops (локальный файл: `artifacts/memory-tower-preparation/completion-memory-ops-audit.md`))
различают механику, измерение полезности и требования включения в default.

Проверенные ограничения и промежуточные ворота собраны в
`feature-inventory.md` (локальный файл: `artifacts/memory-tower-preparation/feature-inventory.md`),
`INTERIM_RESULT.md` (локальный файл: `artifacts/memory-tower-preparation/INTERIM_RESULT.md`) и
`REMAINING_GOAL_GATES.md` (локальный файл: `artifacts/memory-tower-preparation/REMAINING_GOAL_GATES.md`).
Изменений к реализации, конфигурации, sealed-артефактам и запусков модели или
симулятора при подготовке этого отчёта не выполнялось.

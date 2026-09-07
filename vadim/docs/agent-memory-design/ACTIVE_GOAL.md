# Active goal — 2026-09-05

The user explicitly requested formulation and activation of a persistent goal.
No global token budget was requested. Per-experiment bounds remain required.

Довести универсального агента в /Users/mingazhev/Repos/podlodka/uptick/vadim до воспроизводимого выполнения полных задач и провести проверяемую оценку практической пользы памяти. Приоритет — память: сохранение наблюдений и рабочего состояния между решениями, корректное извлечение и использование опыта между прогонами. Handoff, бюджеты и обнаружение отсутствия прогресса улучшать только как средства достижения этой цели.

Критерии завершения: (1) сохранённые повторные полные SRE-прогоны подтверждают выполнение исходной публичной цели/SLO в заранее объявленных бюджетах, а не только достижение горизонта; (2) выполнено заранее зафиксированное парное сравнение без долговременной памяти и с ней при одинаковых модели, инструментах, кратковременном контексте, бюджетах и условиях задач, с отдельными обучением и замороженной оценкой; измерены успешность, стоимость среды, суммарные модельные вызовы/токены и время, ошибки устаревших свидетельств; все попытки сохранены, включая отрицательные; (3) вывод о наличии, отсутствии или неопределённости эффекта соответствует данным, выбранная конфигурация обоснована, универсальность проверена на задаче другого типа, необходимые тесты и ревью пройдены. Отрицательный результат честного сравнения допустим; одних микропроб вместо успешного поведения агента недостаточно. Не выдавать development-данные за независимую оценку и не заявлять обобщение без предусмотренных проектом оснований.

Работать автономно циклами: изучить фактический сбой → сформулировать опровержимую гипотезу → выбрать минимальное изменение универсального механизма → заранее определить различающий эксперимент и его бюджеты → выполнить, проверить происхождение данных и полный результат → принять, пересмотреть или отклонить изменение → немедленно продолжить следующий полезный шаг. Не завершать работу после отдельного теста, отчёта или неудачной попытки. Не повторять дорогие прогоны без нового проверяемого основания. Периодически проверять, что работа приближает north star и сохраняет универсальность. Начать с выводов MECHANISM_RECOVERY.md и OBSERVATION_HANDOFF_RESULTS.md: точное чтение восстановило факты, но увеличило суммарную стоимость; проверить повторное использование свидетельств и навигацию без готового каталога смещений.

Границы: изменения только под vadim/; сохранять чужие и незавершённые правки. Соблюдать существующие архитектурные и оценочные контракты. Не допускать утечки оракула: в решения и память поступают только действительно доступные агенту публичные наблюдения и допустимый обучающий опыт; скрытое состояние симулятора, будущие события, ответы оценщика и подборки по правильному ответу недоступны. Не ослаблять валидаторы/критерии ради положительного результата. Использовать уже разрешённые эксперименты через подписку OpenAI Codex; не расширять разрешение на новые назначения данных, реальные внешние системы или разрушительные операции. Привлекать субагентов к конкретным независимым задачам в рамках ранее разрешённого разделения работы.

Поддерживать компактный HANDOFF.md с текущей гипотезой, доказательствами, отрицательными результатами и следующим действием; подробные трассы хранить в артефактах. Продолжать без повторного разрешения на уже согласованные действия. Не объявлять goal достигнутым, пока критерии не выполнены. При внешнем препятствии сначала исчерпать полезные независимые пути; если продолжение действительно требует пользователя или внешнего изменения, точно зафиксировать блокер, проверенные попытки и минимально необходимый ввод. Не маскировать блокировку или неопределённость успешным завершением.


## Owner priority clarification — 2026-09-06

Before another large SRE/budget/economics experiment, simplify and establish the
memory chain: exact event persistence with provenance/time; relevant factual
retrieval for matching situations and explicit non-applicability/uncertainty for
negative controls; then model use of those facts, including failed outcomes and
stale evidence. Keep evaluator expectations outside model/retrieval inputs.
Finish the already-running frozen pair unchanged. The next experiment should
be small and diagnose memory directly; do not launch the proposed120-minute SRE
run immediately afterward. The larger budget remains a later calibration option.
Mechanical storage checks and isolated successful retrievals do not establish
systematic relevance, model utility, or economic benefit.


## Memory-chain checkpoint and next development run — 2026-09-06

The intervening small checks are complete: exact20record persistence, known
rank1 retrieval,5/5 guided factual answers including failed actions and abstention,
and4/4vs2/4 ordinary-agent recoveries in the controlled family. Independent
reviews passed. These do not establish generalization or full SRE utility.

A separate short-term mechanism now preserves complete redacted observations
using spare history budget without changing baseline selection. The fixed public
prefix and four ordinary-policy decisions showed reproducible evidence retention
and a descriptive action difference. The option is integrated into AgentRunner
without changing persisted AgentConfig or defaults;705tests passed,2live skips.

Next planned experiment is one fresh full SRE development run, not a matched
comparison:480decisions/7200seconds, Terra/medium, same frozen historical corpus
and raw configuration, explicit2000-byte complete views under8000total. The
previous160-decision cap bound after34minutes. Faithful public-state replay atI34
is unavailable because IDs, async operations and clocks are run-specific.

This run tests actual full-task behavior after the memory checks. It changes both
budget and working-memory retention, so no causal comparison with old attempts
will be claimed. Success still requires the full public horizon and SLO. Economic
tuning and another large paired evaluation remain deferred until this gives
useful behavioral evidence. The full original Goal and completion criteria remain
unchanged. All inputs and budget must be reviewed/frozen before dispatch.


## Simulator redeployment and budget approval — 2026-09-06

Owner approved the single480decision/120minute attempt,120seconds per decision,
then requested refreshing the new simulator before running. Old full-sre plan01
is superseded without any model or simulator calls. Public API0.7.1 now defines
cost-bands.v1: full horizon and uptime>=.99 with cost<50million RUB earns100;
[50,150)million earns75,[150,500)million earns50,>=500million earns0.
Incomplete or uptime<.99 earns0. Exactly99% is sufficient. Costs are in kopecks
in the API; future spend is not included in running score upper bounds.
Refresh public tools/briefing and version provenance before the new freeze.
Memory remains the research priority; new scoring changes the public task target,
not the requirement for honest memory evaluation or generic mechanisms.


## Refreshed attempt dispatched — 2026-09-06

Public API0.7.1 and its sanitized startup document have been refreshed; typed
query_logs_summary was implemented and verified against the public server.
Full suite710passed2existinglive skips; source/runner smoke and reviews passed.
New frozen plan: artifacts/simulator-refresh-2026-09-06/full-sre/plan-01.json,
SHA6012d9f830d47780381370bd8e267adb5c8138701d15f1579b274c29a34c2816.
Exactly one authorized attempt started2026-09-05T21:25:29Z:
run cFT5xFNHnrc7tc9kYK4xVH0M, Terra/medium,480decisions/7200s/120s perdecision.
The actual new startup matched before first modelcall. Memory corpus remains
312immutable old public records with explicit oldversion provenance. Score100
requires full horizon,SLO>=.99,RUBcost<5billionminor; lower scores/incomplete/
unknown are retained distinctly. This live attempt has no outcome yet.


## Additional attempt authorization

Owner now explicitly permits up to8full attempts if useful. Count the active
v0.7.1 run as attempt1, leaving7. Retain per-attempt480decisions/7200seconds/
120seconds perlogicaldecision and Codex subscription scope. Do not execute
blind repetitions: each next attempt must have a new distinguishing hypothesis,
preregistered replication, or matched memory-comparison role. All attempts count,
including failed/interrupted ones; current frozen plan remains immutable.


## Current public scoring and prospective milestone — 2026-09-06

The owner supplied the author's new cost-bands.v2 announcement: the imperative
to achieve exactly100/100 was removed. Fresh public OpenAPI fetched at09:41:57UTC
confirms the expanded bands while retaining API version0.8.1 (byte SHA
88fbfa86a6282e7f7f2f723248b586d4b294b43a38b0f9058ba59dc9927c9c1c).
Evidence lives under artifacts/simulator-refresh-080-2026-09-06/scoring-update-20260906/.
Full horizon and uptime>=.99 remain mandatory; score100 still requires cost<50mRUB.
Higher-cost completed SLO passes now earn90/80/70/60/50/40/30/20/10 at successive
exclusive upper bounds100m/150m/200m/300m/500m/1b/2b/3b/5bRUB. API amounts arekopecks.
The next completion milestone is a full SLO pass with the native score and cost
reported separately, not rejection of every outcome below100. Optimize cost among
SLO passes. A future experiment must declare its own target before dispatch;
100points remains an optimization tier, not a substitute for reliable memory
evaluation. Existing frozen plans/classifications are immutable. Attempt5 still
has score0 because uptime26.45% fails both old and new rules.

Five of eight attempts are now closed; three remain unstarted. New public
startup must be captured/validated before a next run's first model call instead
of blindly requiring the old startup SHA. The author's real-time-deadline and
16-hour-jump anecdote is not evidence of SLO success or authorization to force
blind jumps. Real-time execution budgets and simulated-time advances are distinct.
Memory remains the research priority; changed scoring is a new environment
condition, not evidence of memory benefit or a reason to relabel prior runs.

## Further attempt authorization — 2026-09-06T10:15Z

The owner explicitly authorized five additional full attempts. The cumulative
limit is now13: five attempts closed and eight unstarted, including attempt06.
This extends the number of attempts only. Current per-attempt budgets remain
480 decisions,1920 actions,7200 seconds total and120 seconds per logical model
decision. Each run still needs a distinguishing hypothesis, declared replication
or matched-comparison role; retain all failures, and never replace an attempt
automatically. The original completion criteria and oracle boundary are unchanged.
Historical authorization counts above describe their original observation times.

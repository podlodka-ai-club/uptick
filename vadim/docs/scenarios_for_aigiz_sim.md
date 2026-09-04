# Сценарии self-improving агента для симулятора Айгиза

## S1. Неоднозначные HTTP 500

Класс: context-dependent, anti-common-sense, exploration vs exploitation.

Общий старт:

```text
site_status = degraded/unavailable
error_rate растёт
часть страниц отвечает 500
теряются покупки и выручка
```

| Вариант | Отличающий сигнал | Хорошая реакция | Плохая реакция |
|---|---|---|---|
| Органический пик | `SERVER_CAPACITY_EXCEEDED`, высокая utilization, нагрузка следует календарному пику | Рассчитать минимальный временный scale, дождаться activation, затем scale-down | Искать fix-token или держать лишние instances до конца месяца |
| DDoS `scale_or_expiry` | Тот же capacity error, устойчивый аномальный load | Scale, если сохранённая выручка выше стоимости; иначе дождаться expiry | Пытаться применить случайный mitigation fix |
| DDoS `fix_or_expiry` | `DDOS_MITIGATION_REQUIRED`, лог содержит точный `MITIGATE-...` | Применить точный текст и перепроверить страницу | Scale: дополнительные servers не снимают блокировку |
| DDoS `expiry_only` | Attack error, но в логе нет рабочего fix-token | Не сжигать деньги на бесполезный scale; переждать/продвинуть время | Бесконечно scale или перебирать fixes |
| Page bug | `PAGE_BUG`, лог содержит точный `FIX-...` | Применить точный текст, затем probe | Scale или deployment без диагностики |
| Активный deployment | `DEPLOYMENT_ERROR`, deployment/operation имеет `running` | Дождаться завершения, затем проверить итог | Scale во время полного deployment downtime |

Ценность памяти: запомнить условное соответствие между error code/message и классом
действия. Точный token всё равно надо брать из текущих логов: перенос token из другого
seed — вредная память.

## S2. Регрессия после deployment без rollback

Класс: sequence-dependent, delayed consequence, system quirk.

Причинная цепочка:

```text
start deployment
  -> backend недоступен на время operation
  -> deployment succeeds или fails
  -> при success применяются скрытые efficiency effects
  -> иногда активируются новые page bugs
  -> баг проявляется только на следующих visitor/probe requests
```

Хороший playbook:

1. Запускать только `available` deployment и учитывать стоимость downtime.
2. Дождаться terminal status operation, не интерпретировать штатный `DEPLOYMENT_ERROR`
   как новую причину.
3. После success проверить metrics/logs и целевые страницы.
4. При `PAGE_BUG` взять точный fix из текущего лога, применить его и повторить probe.
5. Заново оценить нужное число instances: deployment мог изменить load и hold time.

Память должна хранить playbook и наблюдённые trade-off, но не правило вида
`deployment-002 всегда хороший`: effects детерминированы seed и различаются между
мирами.

## S3. Временный scale как экономическое решение

Класс: equivalent strategies, exploration vs exploitation.

Scale — не безусловно хорошее действие:

- новый instance начинает помогать только через 300 секунд;
- server cost начисляется почасовыми периодами с округлением вверх;
- scale-down занятого instance проходит через draining;
- ожидание теряет выручку, но чрезмерный scale съедает balance;
- при `fix_or_expiry` и `expiry_only` дополнительные instances вообще не устраняют
  блокировку.

Хорошая policy выбирает минимальный `desired_instances`, сравнивает marginal server cost
с ожидаемым lost revenue и сразу планирует условие scale-down. Плохая policy выставляет
фиксированное большое число servers при любом `500`.

Ценность памяти: выучить реальные capacity thresholds страниц, provisioning lag и
экономически разумный запас, а не просто «при перегрузе scale».

## S4. Отложенная цена overprovisioning

Класс: delayed consequence.

Немедленный эффект агрессивного scale выглядит положительно: utilization падает и
запросы снова проходят. Отложенный эффект — повторные почасовые списания, падение
balance и досрочное завершение run при отрицательном балансе.

Для оценки нужен не только технический recovery, но и окно после него:

- время до первого полезного scale;
- lost revenue до activation;
- server cost за следующие billing periods;
- время, проведённое с избыточной capacity;
- итоговый balance и gap до world benchmark.

## S5. Повторяемый календарь нагрузки

Класс: hidden recurring pattern, prevention.

Профиль трафика стабилен по структуре: час дня, день недели, фиксированные праздники и
Black Friday. Конкретный месяц выбирается seed, поэтому память должна обобщать
закономерность, а не запоминать абсолютную дату одного run.

Эволюция поведения:

```text
первые пики: overload -> reactive scale -> часть revenue потеряна
несколько циклов: сопоставление времени, utilization и lost revenue
после обучения: pre-scale не позже чем за provisioning lag
после пика: scale-down до минимальной устойчивой capacity
```

Анти-pattern: постоянно держать capacity для Black Friday. Это предотвращает ошибки, но
из-за server cost проигрывает более точному календарному управлению.

## S6. Приоритет по бизнес-ущербу

Класс: organizational knowledge, equivalent strategies — в ограниченном виде.

При нескольких одновременных проблемах сортировать их только по error rate неверно.
Нужно учитывать:

- `lost_revenue_minor` и `lost_purchases`;
- страницу воронки и количество затронутых запросов;
- стоимость и время remediation;
- временный ли это эффект deployment/attack или постоянный page bug.

Практическое правило: сначала действие с максимальным ожидаемым восстановлением balance
на единицу времени и стоимости.

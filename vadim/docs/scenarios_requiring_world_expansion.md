# Сценарии, требующие расширения мира

## W1. Диск заполнен отладочными логами

Класс: common-sense, sequence-dependent.

Что видит агент:

```text
disk usage: 75% -> 90% -> 97%
latency и error rate растут
часть операций записи завершается ошибкой
```

Скрытая причина: после изменения конфигурации остался включён debug logging. Логи
растут быстрее, чем успевает штатная ротация. После заполнения диска база данных и
сервисы, которым нужна запись, начинают отказывать.

Хорошая реакция:

1. Найти директорию и процесс, создающий основной объём данных.
2. Проверить историю конфигурации.
3. Отключить debug logging.
4. Безопасно ротировать или архивировать диагностические логи.
5. Проверить восстановление свободного места и операций записи.

Плохая реакция: удалять самые большие неизвестные файлы или чистить данные базы без
диагностики.

Ценность памяти: запомнить локальное расположение логов, источник debug-конфигурации и
безопасную процедуру очистки.

Расширение мира: filesystem volumes, disk metrics, классы файлов, logging config,
write failures и действия `disable_debug`, `rotate_logs`, `delete_files` с риском потери
данных.

## W2. Restart billing вызывает cache stampede

Класс: system quirk, anti-common-sense, delayed consequence.

Что видит агент:

```text
billing latency растёт
ошибки пока умеренные
instances выглядят живыми
```

Скрытая особенность: billing держит локальный cache. Restart очищает его, после чего все
instances одновременно запрашивают данные из PostgreSQL. Через несколько минут нагрузка
на базу утраивается и деградируют уже checkout и purchase.

Хорошая реакция: подтвердить состояние billing и базы, устранить исходную причину без
массового restart либо перезапускать instances последовательно с предварительным
cache warm-up.

Плохая реакция: одновременно restart всех billing instances. Немедленно latency может
улучшиться, но отложенный DB overload делает общий ущерб больше.

Ценность памяти: переопределить общий prior «restart — дешёвое безопасное действие»
локальным знанием о cache stampede.

Расширение мира: отдельные services, cache state `warm/cold`, service dependencies,
database load, rolling restart, cache warm-up и отложенные эффекты действий.

## W3. Ошибка после необратимой DB migration

Класс: anti-common-sense, sequence-dependent.

Что видит агент:

```text
после deployment растут application errors
новая версия приложения активна
schema migration завершена
```

Скрытая причина: migration изменила schema так, что предыдущая версия приложения с ней
несовместима. Обычный rollback приложения не восстанавливает сервис, а увеличивает число
ошибок.

Хорошая реакция: проверить версии app и schema, определить compatibility, выполнить
roll-forward на исправленную версию или применить совместимый config/schema fix.

Плохая реакция: автоматически rollback приложения только потому, что инцидент начался
после deployment.

Ценность памяти: запомнить, какие migrations обратимы, compatibility matrix и локальный
roll-forward playbook.

Расширение мира: версии приложения и schema, migrations, compatibility rules,
deployment history, `rollback`, `roll_forward`, migration status и риск data loss.

## W4. Утечка credentials с активными sessions

Класс: sequence-dependent, security, organizational knowledge.

Что видит агент:

```text
secret обнаружен в публичном месте
audit logs показывают подозрительные запросы
часть sessions создана с использованием скомпрометированного ключа
```

Скрытая особенность: простая ротация секрета не завершает уже выданные sessions. Если
сначала выпустить новый ключ, но оставить старый действующим, атакующий продолжит доступ.

Хорошая реакция:

1. Установить затронутый credential и масштаб компрометации.
2. Немедленно revoke старый credential.
3. Завершить связанные sessions и tokens.
4. Выпустить и распространить новый credential.
5. Проверить audit trail и восстановить легитимных consumers.

Плохая реакция: только создать новый secret, не отозвав старый и не завершив sessions;
либо блокировать всех пользователей без оценки масштаба.

Ценность памяти: хранить точный remediation order, список consumers и последствия
ротации для конкретной компании.

Расширение мира: secrets, principals, sessions, audit events, attacker activity,
consumers и действия `revoke_credential`, `kill_sessions`, `rotate_credential`,
`block_principal`.

## W5. Рост очереди: несколько допустимых стратегий

Класс: equivalent strategies, context-dependent.

Что видит агент:

```text
queue lag и oldest message age растут
producer rate превышает consumer rate
ошибок обработки пока нет
```

Варианты контекста:

| Контекст | Хорошая реакция | Цена |
|---|---|---|
| Краткий легитимный пик | Временно scale consumers | Infrastructure cost и DB connections |
| Некритичные события | Временно rate-limit producers | Задержка данных |
| Деградация downstream | Pause consumption или включить degrade mode | Рост lag, но защита downstream |
| Poison messages | Изолировать сообщения в DLQ | Риск пропустить бизнес-событие |

Плохая реакция: всегда агрессивно scale consumers, не проверяя downstream capacity и
природу сообщений.

Ценность памяти: выучить стоимость стратегий, допустимую задержку разных queues и
границы безопасного scale.

Расширение мира: queues, producers, consumers, lag/age metrics, message classes, DLQ,
downstream dependencies и действия `scale_consumers`, `rate_limit_producer`,
`pause_queue`, `move_to_dlq`.

## W6. Consumer scaling исчерпывает DB connections

Класс: delayed consequence, anti-common-sense.

Что видит агент:

```text
queue lag растёт
после scale lag начинает снижаться
через несколько минут растёт DB connection usage
затем checkout получает connection timeout
```

Скрытая особенность: каждый consumer держит несколько постоянных DB connections. При
числе consumers выше локального порога connection pool исчерпывается. Первое действие
выглядит успешным, а основной outage возникает позже в другом сервисе.

Хорошая реакция: проверить DB headroom, увеличить consumers только до безопасного
порога, при необходимости ограничить producers или увеличить DB capacity.

Плохая реакция: увеличить consumers в несколько раз, ориентируясь только на queue lag.

Ценность памяти: связать отложенный checkout outage с более ранним scale и запомнить
безопасный диапазон consumers.

Расширение мира: общая database, connection limits/pools, зависимости consumers и
checkout от DB, delayed resource acquisition и причинная телеметрия между сервисами.

## W7. Одинаковый высокий CPU с разными причинами

Класс: context-dependent, exploration vs exploitation.

Общий старт:

```text
CPU = 95%
latency растёт
HTTP 5xx растут
```

| Причина | Отличающий сигнал | Хорошая реакция |
|---|---|---|
| Bad deployment | Недавний deployment, новый stack trace | Rollback или roll-forward по compatibility |
| Легитимный traffic spike | RPS растёт, структура трафика обычная | Временный scale |
| DDoS | Аномальные источники и endpoints | Rate limit или block |
| Runaway DB query | RPS стабилен, query time/CPU базы растут | Kill query и исправить источник |
| Compromise/miner | Audit/process anomaly, CPU не связан с RPS | Isolate instance, revoke access, remediate |

Плохая реакция: scale при любой причине. Это маскирует runaway query и compromise, но не
устраняет их.

Ценность памяти: хранить условные признаки причин и стоимость диагностических проверок,
не превращая опыт в абсолютное правило.

Расширение мира: per-service CPU, process list, query telemetry, traffic sources,
security/audit signals, deployment history и действия `kill_query`, `rate_limit`,
`isolate_instance`, `rollback`.

## W8. Ночной backup создаёт повторяющуюся DB latency

Класс: hidden recurring pattern, prevention.

Что видит агент:

```text
примерно в одно время растут DB I/O и latency
checkout замедляется
после окончания окна система восстанавливается сама
```

Скрытая причина: backup job запускается около 02:00 и конкурирует с production queries
за I/O. Каждый отдельный инцидент можно пережить локальным scale, но он повторяется.

Хорошая реакция: сопоставить incidents с job history, затем перенести backup в дешёвое
окно, ограничить I/O или изменить расписание.

Плохая реакция: каждую ночь масштабировать application instances, не устраняя источник
конкуренции.

Ценность памяти: обнаружить периодический паттерн и перейти от remediation к prevention.

Расширение мира: scheduled jobs, backup state, DB I/O capacity, job history и действия
`reschedule_job`, `throttle_job`, `pause_job`.

## W9. Cache-warmer v2 отменяет старое правило

Класс: changing world, memory revision.

Первый период:

```text
restart cache -> cold cache -> DB overload
```

После инфраструктурного изменения:

```text
cache-warmer v2 автоматически прогревает cache после restart
restart больше не вызывает прежний side effect
```

Хорошая реакция: заметить смену версии инфраструктуры, перепроверить старое правило и
обновить область его применимости.

Плохая реакция: навсегда избегать restart на основании старого опыта, даже когда он стал
быстрым и безопасным способом восстановления.

Ценность памяти: не только сохранить знание, но и отозвать или версионировать его после
противоречащих наблюдений.

Расширение мира: инфраструктурные epochs/versions, cache warmer, изменение transition
rules между эпизодами, versioned observations и evaluation, пересекающий момент смены
правила.

## W10. Provider latency или локальный connection pool

Класс: exploration vs exploitation, system quirk.

Что видит агент:

```text
payments latency растёт
часть purchase завершается timeout
provider health снаружи неочевиден
```

Вариант A: внешний provider действительно деградировал; failover сокращает ущерб.

Вариант B: provider здоров, а локальный connection pool исчерпан. Failover дорогой и не
устраняет локальную причину.

Хорошая реакция: сравнить local pool metrics, provider probes и историю; выполнить
failover только при подтверждённой внешней деградации, иначе восстановить pool.

Плохая реакция: всегда переключать provider при росте payments latency.

Ценность памяти: выучить, что в этой системе локальный pool — частая причина одинакового
симптома, и сократить число дорогих failovers.

Расширение мира: генерация provider incidents, provider health/probes, connection pools,
несколько providers, routing state, стоимость failover и действия `reset_pool`,
`failover_provider`, `restore_provider`.

## W11. Одновременные инциденты разной бизнес-критичности

Класс: organizational knowledge, equivalent strategies.

Что видит агент:

```text
analytics почти недоступен
checkout умеренно деградирует
технические метрики analytics выглядят хуже
```

Скрытая особенность: analytics допускает многочасовой lag, а каждая ошибка checkout
немедленно теряет заказы. Технически худший сервис не является первым бизнес-приоритетом.

Хорошая реакция: сначала восстановить checkout, затем заняться analytics; при
необходимости временно отключить некритичные consumers, чтобы освободить общие ресурсы.

Плохая реакция: тратить основное время и capacity на analytics только потому, что у него
выше error rate.

Ценность памяти: выучить локальные SLO, допустимые режимы деградации и стоимость отказа
каждого сервиса.

Расширение мира: несколько независимых services, shared resources, service-level SLO,
разный business impact, degrade modes и не раскрываемые напрямую организационные
приоритеты.

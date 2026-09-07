# Uptick

```world
{
  "id": "uptick",
  "description": "Uptick: SRE-симулятор интернет-магазина, обслуживание инфраструктуры и выполнение цели из паспорта API. Задачи про Uptick, uptime, SLO и управление этим симулятором.",
  "bootstrap": ": \"${AK_PARTICIPANT_TOKEN:?Pass participant_token via --options}\"; python3 -c 'import json,os; print(json.dumps({\"seed\":int(os.environ[\"AK_SEED\"]),\"agent_id\":\"ak2-agent\",\"agent_version\":\"1.0\",\"request_id\":os.environ[\"AK_REQUEST_ID\"],\"participant_token\":os.environ[\"AK_PARTICIPANT_TOKEN\"]}))' | curl --fail-with-body --silent --show-error --max-time 60 -X POST \"${AK_ORIGIN}/v2/start\" -H 'Content-Type: application/json' --data-binary @-",
  "defaults": {
    "origin": "http://81.176.229.58:8080"
  },
  "bootstrap_replay_safe": true
}
```

Начальная команда создаёт запуск и возвращает правила, команды и данные доступа.
Цель и протокол изучи из фактического ответа. Средства работы с этим миром создай
самостоятельно в `knowledge/`: скрипты, скил и промпт. Не зашивай стратегию
управления инфраструктурой в скрипты. Не переноси готовые уроки из ak-agent.

Для создания запуска обязателен `--options '{"participant_token":"ВАШ_ТОКЕН"}'`.
Команда передаёт его в JSON-поле `participant_token`; значение не хранится в паспорте мира.

# Учебный мир кухни

```world
{
  "id": "pantry",
  "description": "Локальная учебная симуляция: персонаж голодает, живот урчит, нужно готовить и есть. Проверка переноса агента в мир без серверов, SLO и инфраструктуры.",
  "bootstrap": "curl --fail-with-body --silent --show-error --max-time 10 -X POST \"${AK_ORIGIN}/start\" -H 'Content-Type: application/json' -d \"{\\\"seed\\\":${AK_SEED},\\\"request_id\\\":\\\"${AK_REQUEST_ID}\\\"}\"",
  "defaults": {"origin": "http://127.0.0.1:8097"},
  "bootstrap_replay_safe": true
}
```

Для локальной демонстрации сначала запусти `environment/server.py`.
Адаптирующийся агент получает только ответ начальной команды, без доступа
к исходникам сервера. Интеграцию он должен написать самостоятельно в knowledge/.

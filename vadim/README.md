# Uptick SGR Agent

> **Изолированная рабочая копия Vadim.** Все изменения agent-memory, их тесты и
> экспериментальные артефакты делаются внутри `vadim/`; исходный соседний
> `simple_agent/` не изменяется. Нормативный дизайн находится в
> [`docs/agent-memory-design/`](docs/agent-memory-design/README.md).

Базовый Python-агент для экспериментов команды Uptick. Он управляет
[SRE-симулятором Айгиза](https://github.com/AigizK/HackerSprint2_sim) через публичный
HTTP API и использует тот же основной паттерн, что
[BITGN sample agents](https://github.com/bitgn/sample-agents) и
[sgr-agent-core](https://github.com/vamplabAI/sgr-agent-core): на каждом шаге LLM
возвращает типизированную SGR-схему с оценкой состояния, коротким планом и ровно одним
действием.

Это не обёртка над всем `sgr-agent-core`. Его research runtime, Tavily, FastAPI и MCP
здесь не нужны и сделали бы форки тяжелее. Взята полезная часть архитектуры —
schema-guided decision loop — и отделена от памяти, модели, мира и телеметрии.

## Архитектура

```text
                   ┌──────────────┐
                   │ AgentMemory  │  legacy / episodic / null / ваша
                   └──────▲───────┘
                          │ context + writes
┌─────────────┐    ┌──────┴───────┐    ┌───────────────────┐
│DecisionModel│◄───│  AgentRunner │───►│    Environment    │
│ OpenAI /    │    └──────┬───────┘    │ simulator / fake  │
│ Codex SGR   │                         │                   │
└─────────────┘           │            └───────────────────┘
                          ▼
                   ┌──────────────┐
                   │ RunObserver  │  console / JSONL / ваша
                   └──────────────┘
```

Стабильные порты находятся в `src/uptick_agent/ports.py`:

- `AgentMemory` — runner-facing граница памяти: контекст, legacy-запись,
  структурированный transition, аудит, очистка (если поддерживается) и финализация;
- `Memory` — legacy storage port, подключаемый через `legacy_memory_runtime`;
- `DecisionModel` — получение одного `NextStep` из контекста;
- `Environment` — запуск мира и выполнение типизированных действий;
- `RunObserver` — трассировка шагов и результатов.

Основной цикл в `AgentRunner` не знает ни про OpenAI, ни про HTTP, ни про JSONL. Поэтому
эксперимент с памятью не требует копии агента или изменения симулятора.

Отдельный адаптер исследовательской xMemory подключается через тот же порт:
[`Подключение xMemory`](docs/XMEMORY_INTEGRATION.md). Аудит завершённости и
границ находится в [`ARCHITECTURE_AUDIT.md`](docs/agent-memory-design/ARCHITECTURE_AUDIT.md),
сравнение с соседним агентом — в
[`AGENT_COMPARISON.md`](docs/agent-memory-design/AGENT_COMPARISON.md).

## Быстрый старт

Требуются Python 3.12+ и `uv`.

```bash
uv sync
cp .env.example .env
export OPENAI_API_KEY=...

uv run uptick-agent run --seed 1
```

CLI по умолчанию использует API v2 развёрнутого симулятора. Цель v2 — завершить
симуляцию с uptime не ниже 99%, затем минимизировать стоимость инфраструктуры.
Модель выбирает одну из 18 типизированных команд; доступы к панели и серверам
обрабатываются внутри HTTP-клиента и не передаются модели. Подробности границы
и проверок: [`Simulator v2 adapter`](docs/SIMULATOR_V2_ADAPTER.md).

Старый API сохранён для воспроизведения v1-экспериментов. Для него нужен
совместимый сервер и явный выбор версии:

```bash
uv run uptick-agent run \
  --seed 1 \
  --simulator-api-version v1 \
  --simulator-url http://127.0.0.1:8080 \
  --model gpt-4.1-mini
```

Для OpenAI-compatible провайдера задайте `OPENAI_BASE_URL` и имя его модели.

### Локальный private-pilot с Codex subscription

`--decision-provider codex` выбирает opt-in адаптер официального Python SDK
`openai-codex` и уже выполненного ChatGPT/Codex login. Он не меняет `AgentRunner`,
`DecisionModel` или память: на каждом решении создаётся отдельный ephemeral Codex thread, а ответ
локально проверяется схемой решений выбранной версии симулятора.

Используйте этот путь только как короткий локальный private-pilot на доверенной машине:

```bash
# Нужен отдельно установленный официальный Codex CLI в PATH:
# https://developers.openai.com/codex/cli/
codex --version

# Выполните login самостоятельно; приложение никогда не запускает эту команду.
codex login

# Установите opt-in dependency только для Codex provider.
uv sync --extra codex

# Не задавайте OPENAI_API_KEY или CODEX_API_KEY: Codex provider откажется стартовать,
# чтобы случайно не перейти на API billing.
unset OPENAI_API_KEY CODEX_API_KEY

# Сначала один seed без памяти.
uv run --extra codex uptick-agent run --seed 42 --memory none \
  --decision-provider codex --simulator-api-version v2 --max-steps 40
```

`CODEX_MODEL` необязателен: без него Codex выбирает свой default; `--model` явно
переопределяет его. `OPENAI_BASE_URL` относится только к OpenAI provider и не передаётся
Codex. CLI наследует reasoning effort из локальной конфигурации Codex: выбранная
модель должна его поддерживать. В диагностическом пилоте `gpt-5.4-mini` отклонила
`max`; отдельный wrapper использовал `low` (см. запись пилота в документации v2).
Перед каждым решением provider проверяет, что локальная Codex account session имеет
тип `chatgpt`, и отказывает API-key/пустому account state. Subscription use расходует
allowance ChatGPT/Codex и не безлимитен. Для shared, CI или high-volume прогонов
рекомендуется обычный API key provider.

Перед запуском Codex получает process-level overrides, которые очищают `mcp_servers` и
отключают web/MCP/shell/browser tool families; adapter не передаёт dynamic tools. Он также
работает в изолированном пустом Git workspace с `read_only` sandbox и `deny_all` approvals.
Проверка событий после turn — дополнительная защита, а не разрешение на tool use. Не
добавляйте в локальный Codex config обходные MCP/tools для этого pilot.

Никогда не коммитьте, не логируйте и не передавайте `auth.json` или другой auth state.
Не используйте личный subscription-auth workflow в CI этого публичного/open-source
репозитория.

## Память

Доступны три CLI-профиля legacy-памяти:

```bash
# Контрольная группа: нет долговременной памяти
uv run uptick-agent run --seed 1 --memory none

# Только память процесса
uv run uptick-agent run --seed 1 --memory in-memory

# Долговременная память между запусками
uv run uptick-agent run --seed 1 --memory jsonl --memory-file memory.jsonl
```

Короткий рабочий контекст последних шести действий и структурированное состояние run
(применённые фиксы, масштабирование, деплои и статусы операций) передаются модели всегда.
Поэтому `--memory none` подключает `legacy_memory_runtime(None)`: долговременная память
отключена, но не превращает каждое решение в амнезийный первый шаг.

`InMemoryMemory` и `JsonlMemory` используют намеренно простой и прозрачный baseline:
лексическая релевантность + важность + свежесть + бонус текущему run. Между run'ами
recall берёт только релевантные записи завершённых запусков; незавершённые следы и записи
с нулевым лексическим пересечением отбрасываются. Это отправная точка, а не претензия на
хорошую когнитивную архитектуру.

Чтобы проверить другую legacy-память, реализуйте три async-метода и подключите объект через
`legacy_memory_runtime`; `AgentRunner` при этом продолжает работать только с `AgentMemory`:

```python
class MyMemory:
    async def remember(self, entry: MemoryEntry) -> None: ...
    async def recall(self, query: MemoryQuery) -> list[MemoryMatch]: ...
    async def clear(self, run_id: str | None = None) -> None: ...
```

Пример обёртки, фильтрующей слабые воспоминания, находится в
`examples/importance_memory.py`. Через этот же контракт можно подключить embeddings,
XMemory, TencentDB Agent Memory или собственную консолидацию lessons.

Stage 4 также добавляет первый структурированный episodic-профиль. Он хранит
полные `ExperienceTransition` и `RunOutcome` в generic store, а в prompt отдаёт
ограниченное untrusted-представление. Credential-shaped значения удаляются до
вычисления provenance hash и записи; остальное raw-содержимое сохраняется.
Пока профиль подключается только программно:

```python
from uptick_agent.memory import episodic_memory_runtime
from uptick_agent.memory.stores import SqliteStructuredStore

memory = episodic_memory_runtime(
    SqliteStructuredStore("artifacts/pilot/episodes.sqlite"),
    namespace="pilot-2026-09-04",
)
```

Для каждого независимого эксперимента нужен новый namespace. У structured store
пока нет безопасного delete/reset API, поэтому `memory.clear()` явно завершится
ошибкой, а CLI/benchmark режим для persistent episodic memory намеренно не
предлагается. Это не ограничивает обычный запуск `AgentRunner` с созданным выше
runtime.

Stage 5 подключает структурированный аудит к тому же runtime:

```python
from uptick_agent.memory import (
    AuditConfiguration,
    MemoryConfiguration,
    StructuredAuditTraceSink,
    episodic_memory_runtime,
)
from uptick_agent.memory.stores import SqliteStructuredStore

configuration = MemoryConfiguration.episodic_only(
    audit=AuditConfiguration.simulator_default(),
)
store = SqliteStructuredStore("artifacts/pilot/memory.sqlite")
audit = StructuredAuditTraceSink(
    store,
    namespace="pilot-2026-09-04-audit",
    configuration=configuration.audit,
    runtime_configuration_fingerprint=configuration.fingerprint,
)
memory = episodic_memory_runtime(
    store,
    namespace="pilot-2026-09-04-episodes",
    configuration=configuration,
    audit_sink=audit,
)
```

Аудит связывает выбор памяти, запрос модели, выбранное действие, созданный эпизод
и исход run. `audit.raw_content.prompts`, `observations` и `decision_traces`
независимо управляют телами аудита; первичные записи памяти сохраняют свою
структуру. Проверка секретов обязательна перед записью. Политика хранения
объявлена в конфигурации; автоматического удаления пока нет. Точные границы и
поведение при сбоях описаны в
[`STAGE_5_IMPLEMENTATION.md`](docs/agent-memory-design/STAGE_5_IMPLEMENTATION.md).

## A/B-прогоны

```bash
uv run uptick-agent benchmark \
  --name v2-no-memory-smoke \
  --simulator-api-version v2 \
  --seeds 1,2,3,4,5 \
  --memory none

uv run uptick-agent benchmark \
  --name v2-jsonl-smoke \
  --simulator-api-version v2 \
  --seeds 1,2,3,4,5 \
  --memory jsonl \
  --memory-file artifacts/v2-jsonl-smoke/memory.jsonl
```

По умолчанию память очищается перед каждым seed: иначе порядок миров влияет на результат
и сравнение нечестно. Для отдельного эксперимента с переносом опыта включите
`--carry-memory`.

Артефакты сохраняются в `artifacts/<experiment>/`:

- `trace.jsonl` — решение, действие, результат и длительность каждого шага;
- `summary.json` — результаты всех seeds; для v2 — число завершённых прогонов,
  прошедших SLO, и их средняя стоимость, для v1 — агрегаты по итоговому балансу.

`summary.json` пока является удобным smoke-сравнением, а не полным evaluation manifest:
его нельзя использовать как validation/promotion evidence. Программный аудит Stage 5
не добавляет отсутствующий evaluation manifest в legacy CLI.

`RunResult.objective_kind` различает v2 `uptime_cost` и v1 `balance`. Для v2
сравниваются `uptime_ratio`, `slo_passed` и `total_cost_minor`; незавершённый run
не считается прошедшим SLO. Поля выручки, покупок и баланса сохраняются для v1.
Число шагов и реальная длительность доступны для обеих версий.

## Где форкать эксперимент

Не копируйте `AgentRunner`. Меняйте только компонент, который проверяете:

- новый prompt/model policy — реализуйте `DecisionModel`;
- другой способ запоминания или recall — реализуйте legacy `Memory` и подключите его через
  `legacy_memory_runtime`;
- другая симуляция — реализуйте `Environment`;
- новые метрики/MLflow/Langfuse — реализуйте `RunObserver`;
- новые команды v2 — добавьте тип в `src/uptick_agent/v2_actions.py` и dispatch в
  `src/uptick_agent/simulator/v2_environment.py`; общие действия находятся в
  `src/uptick_agent/models.py`, а v1 dispatch — в `simulator/environment.py`.

Так различие между ветками остаётся явным, а результаты можно воспроизводить на одном
наборе seeds.

## Проверки

```bash
# Полный offline-набор, включая fake Codex SDK.
env -u OPENAI_API_KEY -u CODEX_API_KEY \
  uv run --extra codex --locked pytest -q -ra

uv run ruff check .
uv run ruff format --check .
```

По умолчанию live-тесты пропущены: HTTP-контракт проверяется через
`httpx.MockTransport`, а цикл агента — через scripted model и fake environment.
Опциональный v2 live-тест запускается командой ниже. Он создаёт run на указанном
сервере, но не вызывает LLM. Для отдельного v1 live-теста используется переменная
`UPTICK_INTEGRATION_SIMULATOR_URL` и совместимый v1 сервер.

```bash
SIMULATOR_V2_URL=http://81.176.229.58:8080 \
  uv run --extra codex --locked pytest -q tests/test_integration_simulator_v2.py
```

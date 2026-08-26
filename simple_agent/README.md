# Uptick SGR Agent

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
                   │    Memory    │  null / RAM / JSONL / ваша
                   └──────▲───────┘
                          │ recall + remember
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

- `Memory` — сохранение, поиск и очистка опыта;
- `DecisionModel` — получение одного `NextStep` из контекста;
- `Environment` — запуск мира и выполнение типизированных действий;
- `RunObserver` — трассировка шагов и результатов.

Основной цикл в `AgentRunner` не знает ни про OpenAI, ни про HTTP, ни про JSONL. Поэтому
эксперимент с памятью не требует копии агента или изменения симулятора.

## Быстрый старт

Требуются Python 3.12+ и `uv`.

```bash
uv sync
cp .env.example .env
export OPENAI_API_KEY=...

uv run uptick-agent run --seed 1
```

По умолчанию используется развернутый симулятор из командного чата. Локальный адрес:

```bash
uv run uptick-agent run \
  --seed 1 \
  --simulator-url http://127.0.0.1:8080 \
  --model gpt-4.1-mini
```

Для OpenAI-compatible провайдера задайте `OPENAI_BASE_URL` и имя его модели.

### Локальный private-pilot с Codex subscription

`CodexSGRModel` — opt-in адаптер для официального Python SDK `openai-codex` и уже
выполненного ChatGPT/Codex login. Он не меняет `AgentRunner`, `DecisionModel`, память
или симулятор: на каждом решении создаётся отдельный ephemeral Codex thread, а ответ
локально проверяется строгой схемой `NextStep`.

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
uv run --extra codex uptick-agent run --seed 1 --memory none --decision-provider codex
```

`CODEX_MODEL` необязателен: без него Codex выбирает свой default; `--model` явно
переопределяет его. `OPENAI_BASE_URL` относится только к OpenAI provider и не передаётся
Codex. Перед каждым решением provider проверяет, что локальная Codex account session имеет
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

Доступны три реализации:

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
Поэтому `--memory none` отключает только recall через `Memory`, а не превращает каждое
решение в амнезийный первый шаг.

`InMemoryMemory` и `JsonlMemory` используют намеренно простой и прозрачный baseline:
лексическая релевантность + важность + свежесть + бонус текущему run. Между run'ами
recall берёт только релевантные записи завершённых запусков; незавершённые следы и записи
с нулевым лексическим пересечением отбрасываются. Это отправная точка, а не претензия на
хорошую когнитивную архитектуру.

Чтобы проверить другую память, достаточно реализовать три async-метода:

```python
class MyMemory:
    async def remember(self, entry: MemoryEntry) -> None: ...
    async def recall(self, query: MemoryQuery) -> list[MemoryMatch]: ...
    async def clear(self, run_id: str | None = None) -> None: ...
```

Пример обёртки, фильтрующей слабые воспоминания, находится в
`examples/importance_memory.py`. Через этот же контракт можно подключить embeddings,
XMemory, TencentDB Agent Memory или собственную консолидацию lessons.

## A/B-прогоны

```bash
uv run uptick-agent benchmark \
  --name baseline-no-memory \
  --seeds 1,2,3,4,5 \
  --memory none

uv run uptick-agent benchmark \
  --name jsonl-memory-v1 \
  --seeds 1,2,3,4,5 \
  --memory jsonl \
  --memory-file artifacts/jsonl-memory-v1/memory.jsonl
```

По умолчанию память очищается перед каждым seed: иначе порядок миров влияет на результат
и сравнение нечестно. Для отдельного эксперимента с переносом опыта включите
`--carry-memory`.

Артефакты сохраняются в `artifacts/<experiment>/`:

- `trace.jsonl` — решение, действие, результат и длительность каждого шага;
- `summary.json` — результаты всех seeds и агрегаты по итоговому балансу.

Ключи сравнения уже нормализованы в `RunResult`: баланс, выручка, потерянная выручка,
стоимость серверов и деплоев, покупки, число шагов и реальная длительность.

## Где форкать эксперимент

Не копируйте `AgentRunner`. Меняйте только компонент, который проверяете:

- новый prompt/model policy — реализуйте `DecisionModel`;
- другой способ запоминания или recall — реализуйте `Memory`;
- другая симуляция — реализуйте `Environment`;
- новые метрики/MLflow/Langfuse — реализуйте `RunObserver`;
- новые действия текущего мира — добавьте Pydantic action в `models.py` и dispatch в
  `simulator/environment.py`.

Так различие между ветками остаётся явным, а результаты можно воспроизводить на одном
наборе seeds.

## Проверки

```bash
# Базовый OpenAI path без optional Codex dependency.
uv run pytest

# Codex adapter tests используют fake SDK и не делают model/simulator calls.
uv run --extra codex pytest tests/test_codex.py

uv run ruff check .
uv run ruff format --check .
```

Тесты не обращаются к LLM и развернутому симулятору: HTTP-контракт проверяется через
`httpx.MockTransport`, а цикл агента — через scripted model и fake environment.

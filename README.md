# Uptick agents

Этот репозиторий рассчитан на несколько независимых вариантов агента.

| Агент | Описание |
| --- | --- |
| [`simple_agent/`](simple_agent/README.md) | Базовый SGR-агент для экспериментов с Uptick SRE-симулятором. |
| [`ak-agent/`](ak-agent/README.md) | Обучающийся SRE-агент: Codex SDK через подписку, постоянная память, CLI и эксперименты API v2. |

Каждый агент — самостоятельный Python-проект со своим `pyproject.toml`, lockfile,
исходниками, тестами и документацией. Для запуска базового варианта:

```bash
cd simple_agent
uv sync
uv run uptick-agent run --seed 1
```

# Uptick agents

Этот репозиторий рассчитан на несколько независимых вариантов агента.

| Агент | Описание |
| --- | --- |
| [`simple_agent/`](simple_agent/README.md) | Базовый SGR-агент для экспериментов с Uptick SRE-симулятором. |

Каждый агент — самостоятельный Python-проект со своим `pyproject.toml`, lockfile,
исходниками, тестами и документацией. Для запуска базового варианта:

```bash
cd simple_agent
uv sync
uv run uptick-agent run --seed 1
```

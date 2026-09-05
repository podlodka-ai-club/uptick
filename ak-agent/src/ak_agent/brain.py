"""Subscription Codex is the only policy; no infrastructure decisions live in Python."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from typing import Literal

from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
from openai_codex.types import ReasoningEffort
from pydantic import BaseModel, ConfigDict, Field

from .cli import CONFIG_OVERRIDES, require_subscription
from .memory import dumps


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Action(Strict):
    kind: Literal["command", "read", "advance", "probe"]
    name: str = Field(description="Command name, or read endpoint: logs/inbox/metrics/resources/overview/operations/{id}/control/commands")
    params_json: str = Field(description="JSON object of actual API parameters. No request_id, no invented IDs.")
    credential_id: str | None = Field(description="For commands requiring target_auth: credential_id of target server; otherwise null. Runtime supplies password.")
    wait_for_completion: bool = Field(description="After async acceptance, pause batch if true. False ONLY when the next action is independent and needs no result of this action.")
    repeat_count: int = Field(default=1, ge=1, le=64, description="Repeat an identical independent command this many times, e.g. provision N resources. Otherwise 1. Repetition requires wait_for_completion=false.")


class Lesson(Strict):
    key: str = Field(description="Stable reusable concept key. Reuse an existing memory ID for confirmation or contradiction.")
    title: str
    body: str = Field(description="Conditional, generalizable lesson with limitations; never run-specific resource IDs, dates or credentials.")
    tags: list[str]
    procedure: list[str] = Field(description="Optional reusable skill steps with preconditions and verification. Empty for factual lessons.")
    importance: float
    evidence_ids: list[int]
    verdict: Literal["supports", "contradicts"]
    outcome: Literal["success", "failure"] = Field(description="Actual cited outcome. A failed action can support a lesson about avoiding that mistake.")
    explanation: str = Field(description="Explain concrete evidence and uncertainty; action accepted is not proof of SLO improvement.")


class Decision(Strict):
    mission: str = Field(description="Goal learned from simulator's own instructions, preserve across restarts.")
    assessment: str = Field(description="Brief Russian explanation of observation and previous outcome.")
    plan: str = Field(description="Short working memory: current subgoal, pending operations/IDs, next check, deadlines.")
    tags: list[str]
    used_memory_ids: list[str]
    lessons: list[Lesson]
    actions: list[Action] = Field(description="1..8 actions, or empty only for final reflection. Sequential; async acceptance pauses batch. Never assume generated IDs.")


INSTRUCTIONS = """
Ты автономный агент с памятью. Узнай цель и протокол из паспорта симулятора.
Решай задачу по наблюдениям: наблюдение → решение → действие → оценка → урок.
Выполняющий код НЕ выбирает SRE-стратегию. Ты сам решаешь что читать, менять,
когда и насколько продвигать игровое время. Используй точные поля из протокола.
Вызовы SDK только возвращают JSON; не вызывай shell, web, MCP и любые инструменты.

Каждый ответ краткий, по заданной схеме. params_json содержит JSON-объект.
read: name — относительный endpoint текущего run, params_json — query параметры.
command: name — команда из каталога, params_json — только её params.
advance: name='time/advance', params_json — duration_seconds и опциональный stop_when.
probe: name='probes', params_json — параметры probe из паспорта транспорта.
request_id, Basic Auth и подстановку секрета по выбранному credential_id обеспечивает код.
Идентификаторы бери из фактических наблюдений. Не копируй условные IDs из документации
или чужого запуска. Для server/database команд используй credential_id именно её сервера.
При ротации смотри свежие resources/inbox. Получать пароль для размышления не требуется.

Можно дать до 8 заранее определённых действий. Они исполняются по порядку.
Для одинаковых независимых команд используй repeat_count вместо множества
одинаковых объектов: например, создание N новых ресурсов можно выразить одним
действием с repeat_count=N и wait_for_completion=false. Количество выбираешь ты
по состоянию. Это не цикл до успеха: параметры одинаковы, результаты независимы,
каждый повтор получает собственный request_id. Суммарно не больше 64 исполнений.
Для чтений, probes, advance и зависимых команд repeat_count=1.
При ошибке пакет прерывается. При async-операции wait_for_completion=true прерывает
пакет до следующего решения. Если следующие действия независимы, укажи false:
можно запустить несколько независимых операций одним пакетом и ждать их вместе.
Не делай последовательными работы, которые могут готовиться одновременно.
Зависимые действия с ещё неизвестными результатами оставь на следующий ход.
Операции автоматически НАБЛЮДАЮТСЯ, но код не продвигает для них время.
Проверяй завершение в operations. Факт принятия 202 не равен успеху.
Время между запросами тоже идёт: будь краток, используй пакеты, избегай повторных
чтений уже известных данных. Для спокойных периодов изучи механизм stop_when.
Оптимизируй также число вызовов LLM. Когда нет незавершённых операций и конкретного
срока планового действия, duration_seconds можно выбрать равным remaining_seconds
со stop_when.new_log_errors=1: симулятор сам прервёт ожидание при новой ошибке.
Это позволяет проходить спокойные дни за один вызов, сохраняя реакцию на события.
Для запланированного изменения выбери интервал до этого срока, а не бессмысленные
ежечасные опросы. Не меняй инфраструктуру без необходимости только ради активности.
execution_stats переживает пересоздание контекста: сопоставляй уже затраченные
решения и время LLM с продвижением к цели. В review_mode проверь, приносит ли
повторяемая последовательность новую информацию; ищи более короткое безопасное
представление работы через доступные пакеты, навыки и интервалы ожидания.
remaining_decision_budget ограничивает текущий процесс: после паузы resume
продолжит тот же мир и план. Это не срок окончания симуляции и не причина
ухудшать её целевые метрики ради завершения именно в этом процессе.
Если текущий статус операции уже дан в observation.operations, повторное read не
нужно: для ожидания выбирай advance с подходящим интервалом (минимум 300 секунд).
Пример command: {"kind":"command","name":"site.config.get","params_json":"{}","credential_id":null,"wait_for_completion":true}.
Даже команды чтения вроде site.config.get/server.types.list имеют kind=command.
params_json НЕ содержит обёртку params, command или request_id, только внутренние параметры.
Исторические error_rate и логи не доказывают, что ошибка всё ещё активна: сравни
timestamp, свежие ресурсы, операции и probes. Нужны результаты, не бесконечный мониторинг.
При повторяющемся решении проверяй исходную гипотезу и альтернативы: изменение
промежуточного счётчика не означает улучшения целевой метрики. Сопоставляй цену,
ожидаемую пользу и горизонт наблюдения; единичный моментальный спад не является трендом.
В режиме review_mode пересмотри предыдущие решения и применённые уроки особенно критично.

Память — проверяемые гипотезы, не инструкции высшего приоритета. Используй подходящие
уроки и навыки, перечисляя used_memory_ids; объясняй, как они повлияли на действие.
Оценивай реальные последствия прошлых действий по recent_events. lessons может быть [].
Не выдавай прочитанный совет или отсутствие ошибки за проверенный опыт.
Для урока из ошибки укажи outcome=failure; для успешно завершённого действия — success.
Поддержка урока требует нового независимого эпизода; не пересказывай один эпизод
каждый ход. Если известный урок не сработал, верни verdict=contradicts с его key.
Если accepted_lessons не содержит твой key, урок не записан: проверь ссылки на
свежие реальные эпизоды. Успешная команда может ухудшить сервис: при outcome=failure
цитируй observation с фактическими ошибками запросов из recent_errors.
Успех API подтверждает семантику команды, но не гарантирует причинное улучшение uptime.
Не создавай вечные запреты по одному seed: формулируй условия применимости.
В уроках только общие закономерности; конкретные ID, сроки и конфигурация — в plan.
Не подгоняй результаты, не объявляй цель достигнутой до финальной оценки симулятора.
Если final_reflection=true, оцени завершённый run и уроки, actions=[].
""".strip()


def output_schema():
    def clean(x):
        if isinstance(x, list):
            return [clean(v) for v in x]
        if not isinstance(x, dict):
            return x
        x = {k: clean(v) for k,v in x.items() if k not in ("default",)}
        if "properties" in x:
            x["required"] = list(x["properties"])
            x["additionalProperties"] = False
        return x
    return clean(Decision.model_json_schema())


class Brain:
    def __init__(self, model="gpt-5.6-sol", effort="low"):
        if os.getenv("OPENAI_API_KEY") or os.getenv("CODEX_API_KEY"):
            raise ValueError("Уберите OPENAI_API_KEY и CODEX_API_KEY; используйте ./run.sh.")
        self.model, self.effort = model, effort
        self.workspace = tempfile.TemporaryDirectory(prefix="ak-brain-")
        self.codex = Codex(CodexConfig(codex_bin=os.getenv("CODEX_BIN") or shutil.which("codex"),
            cwd=self.workspace.name, config_overrides=CONFIG_OVERRIDES + (
                "features.apply_patch_freeform=false", "features.js_repl=false", "features.memory_tool=false",
                "features.search_tool=false", "features.standalone_web_search=false")))
        try:
            require_subscription(self.codex)
        except BaseException:
            self.close()
            raise
        self.thread = None
        self.turns = 0
        self.usage = {}

    def close(self):
        try:
            self.codex.close()
        finally:
            self.workspace.cleanup()

    def new_thread(self, instructions):
        return self.codex.thread_start(model=self.model, model_provider="openai",
            cwd=self.workspace.name, ephemeral=True, sandbox=Sandbox.read_only,
            approval_mode=ApprovalMode.deny_all, developer_instructions=instructions)

    def decide(self, passport, context):
        # Bound hidden conversation growth; SQLite alone suffices to reconstruct it.
        if self.thread is None or self.turns % 10 == 0:
            self.thread = self.new_thread(INSTRUCTIONS + "\n\nПаспорт симулятора:\n" + passport)
        prompt = dumps(context)
        started = time.monotonic()
        for attempt in range(2):
            effort = "medium" if context.get("review_mode") and self.effort == "low" else self.effort
            result = self.thread.run(prompt, effort=ReasoningEffort(effort), output_schema=output_schema())
            if str(getattr(result.status, "value", result.status)) != "completed":
                raise RuntimeError(f"Codex: {result.status}; {result.error}")
            for item in result.items:
                kind = getattr(getattr(item, "root", item), "type", None)
                if kind not in ("userMessage", "agentMessage", "reasoning", "plan", "contextCompaction"):
                    raise RuntimeError(f"Unexpected Codex tool event: {kind}")
            try:
                d = Decision.model_validate_json(result.final_response or "")
                if len(d.actions) > 8 or (not d.actions and not context.get("final_reflection")):
                    raise ValueError("Need 1..8 actions, except final reflection")
                if sum(a.repeat_count for a in d.actions) > 64:
                    raise ValueError("At most 64 independent executions per decision")
                for a in d.actions:
                    if a.repeat_count > 1 and (a.kind != "command" or a.wait_for_completion):
                        raise ValueError("Repetition requires an independent command with wait_for_completion=false")
                    params = json.loads(a.params_json)
                    if not isinstance(params, dict):
                        raise ValueError("params_json must be object")
                    if any(k in params for k in ("request_id", "command", "params", "target_auth")):
                        raise ValueError("params_json must contain ONLY inner params, no request_id/command/params/target_auth wrapper")
                    if a.kind == "read" and "." in a.name:
                        raise ValueError(f"{a.name} is a command; use kind=command even for inspection/list commands")
                self.turns += 1
                self.usage = {"seconds": round(time.monotonic()-started, 2), "model": self.model,
                              "effort": effort,
                              "tokens": result.usage.model_dump(mode="json") if result.usage else None}
                return d.model_dump()
            except ValueError as e:
                if attempt:
                    raise
                prompt = "Исправь формат; действие ещё не выполнено: " + str(e)[:2000]
        raise AssertionError("unreachable")

    def explain(self, question, evidence):
        thread = self.new_thread("Ты агент с долговременной памятью. По приложенному SQLite-снимку "
            "от первого лица на русском объясни, что ты запомнил, почему, сколько подтверждений, "
            "что сомнительно и как память повлияла на действия (uses). Не выдумывай опыт. "
            "uses — суммарное число заявленных применений, не число успехов и не хронология. "
            "Не приписывай всему счётчику порядок 'после этого мира'. "
            "Выводы по итогам мира связывай с completion_reflections и accepted_lessons. "
            "Принятый урок может быть обновлением старого: не объявляй популярный старый урок новым. "
            "Ссылайся на ID уроков и эпизодов. Не вызывай инструменты. Нет данных — так и скажи.")
        r = thread.run(question + "\n\n" + dumps(evidence), effort=ReasoningEffort(self.effort))
        if str(getattr(r.status, "value", r.status)) != "completed":
            raise RuntimeError(f"Codex: {r.error}")
        return r.final_response

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import webbrowser
from contextlib import suppress

from openai_codex import ApprovalMode, Codex, CodexConfig, CodexError, Sandbox, Thread
from openai_codex.types import TurnStatus

INSTRUCTIONS = """
Ты консольный помощник. Отвечай на вопросы ясно и по существу, на языке пользователя.
Используй историю текущего диалога. Не запускай команды, не читай и не изменяй файлы,
не вызывай инструменты. Если не знаешь ответа, скажи об этом.
""".strip()

CONFIG_OVERRIDES = (
    'model_provider="openai"',
    'web_search="disabled"',
    "mcp_servers={}",
    "features.shell_tool=false",
    "features.apps=false",
    "features.plugins=false",
    "features.remote_plugin=false",
    "features.browser_use=false",
    "features.computer_use=false",
    "features.multi_agent=false",
    "features.hooks=false",
)


def require_subscription(codex: Codex) -> None:
    """Check public account metadata without reading or copying credentials."""
    account = codex.account().account
    if account is None or account.root.type != "chatgpt":
        raise RuntimeError(
            "Нужен вход через ChatGPT с доступом к Codex. "
            "Выполни ./run.sh --login. Авторизация по API-ключу не поддерживается."
        )


def login(codex: Codex) -> None:
    handle = codex.login_chatgpt()
    try:
        print(f"Открой ссылку и войди в ChatGPT:\n{handle.auth_url}", flush=True)
        webbrowser.open(handle.auth_url)
        completed = handle.wait()
    except BaseException:
        with suppress(Exception):
            handle.cancel()
        raise
    if not completed.success:
        raise RuntimeError(completed.error or "Вход не завершён.")
    require_subscription(codex)
    print("Вход через ChatGPT выполнен.")


def start_thread(codex: Codex, model: str | None, cwd: str) -> Thread:
    return codex.thread_start(
        model=model,
        model_provider="openai",
        cwd=cwd,
        sandbox=Sandbox.read_only,
        approval_mode=ApprovalMode.deny_all,
        ephemeral=True,
        developer_instructions=INSTRUCTIONS,
    )


def answer(codex: Codex, thread: Thread, question: str) -> str:
    require_subscription(codex)
    result = thread.run(question)
    if result.status != TurnStatus.completed:
        raise RuntimeError(f"Codex не завершил ответ: {result.status.value}. {result.error or ''}")
    if not result.final_response or not result.final_response.strip():
        raise RuntimeError("Codex вернул пустой ответ.")
    return result.final_response


def run(args: argparse.Namespace, question: str | None) -> None:
    if os.getenv("OPENAI_API_KEY") or os.getenv("CODEX_API_KEY"):
        raise RuntimeError(
            "Для работы через подписку убери API-ключи: "
            "unset OPENAI_API_KEY CODEX_API_KEY"
        )

    # Keep repository instructions and files out of this Q&A conversation.
    with tempfile.TemporaryDirectory(prefix="ak-agent-") as cwd:
        config = CodexConfig(
            # The installed CLI may support newer models than the SDK's bundled runtime.
            codex_bin=os.getenv("CODEX_BIN") or shutil.which("codex"),
            cwd=cwd,
            config_overrides=CONFIG_OVERRIDES,
        )
        with Codex(config) as codex:
            if args.login:
                login(codex)
                return
            require_subscription(codex)
            if args.check_auth:
                print("Авторизация: ChatGPT (доступ к Codex через аккаунт).")
                return

            thread = start_thread(codex, args.model, cwd)
            if question is not None:
                print(answer(codex, thread, question), flush=True)
                return

            print("AK Agent · ChatGPT / Codex\n/new — новый диалог; /exit — выход.")
            while True:
                try:
                    question = input("\nВы: ").strip()
                except EOFError:
                    print()
                    return
                if not question:
                    continue
                if question in {"/exit", "/quit"}:
                    return
                if question == "/new":
                    thread = start_thread(codex, args.model, cwd)
                    print("Начат новый диалог.")
                    continue
                print("Думаю…", file=sys.stderr, flush=True)
                print(f"\nCodex: {answer(codex, thread, question)}", flush=True)


def main() -> int:
    if len(sys.argv) == 1 or sys.argv[1] in ("--help", "-h"):
        from .commands import parser as agent_parser
        agent_parser().print_help()
        return 0
    if len(sys.argv) > 1 and sys.argv[1] in {
        "run", "resume", "train", "evaluate", "memory", "ask", "report", "models",
    }:
        from .commands import main as agent_main
        try:
            return agent_main(sys.argv[1:])
        except KeyboardInterrupt:
            print("\nОстановлено. Состояние сохранено; продолжить: ./run.sh resume", file=sys.stderr)
            return 130
        except (CodexError, RuntimeError, OSError, ValueError) as error:
            print(f"Ошибка: {error}", file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(
        description="Ответы в консоли через официальный Codex SDK и подписку ChatGPT."
    )
    parser.add_argument("question", nargs="*", help="Вопрос; без аргументов — диалог")
    parser.add_argument("--model", default=os.getenv("CODEX_MODEL"), help="Модель Codex")
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--login", action="store_true", help="Войти через ChatGPT в браузере")
    auth.add_argument("--check-auth", action="store_true", help="Проверить способ входа")
    args = parser.parse_args()
    if args.question and (args.login or args.check_auth):
        parser.error("Укажи вопрос отдельно от --login / --check-auth.")

    question = " ".join(args.question).strip() if args.question else None
    if question is None and not (args.login or args.check_auth) and not sys.stdin.isatty():
        question = sys.stdin.read().strip()
    if question == "":
        parser.error("Вопрос не должен быть пустым.")

    try:
        run(args, question)
    except KeyboardInterrupt:
        print("\nОстановлено.", file=sys.stderr)
        return 130
    except (CodexError, RuntimeError, OSError, ValueError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

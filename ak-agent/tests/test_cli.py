import argparse
import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from openai_codex import TurnResult
from openai_codex.types import TurnStatus

from ak_agent import cli


def client(account_type="chatgpt"):
    codex = MagicMock()
    account = None if account_type is None else SimpleNamespace(
        root=SimpleNamespace(type=account_type)
    )
    codex.account.return_value = SimpleNamespace(account=account)
    return codex


def completed(text="Ответ"):
    return TurnResult(
        id="test-turn", status=TurnStatus.completed, error=None,
        started_at=None, completed_at=None, duration_ms=None,
        final_response=text, items=[], usage=None,
    )


class CliTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(cli.os.environ, {}, clear=True).start()
        self.output = patch("sys.stdout", new_callable=io.StringIO).start()
        patch("sys.stderr", new_callable=io.StringIO).start()
        self.args = argparse.Namespace(login=False, check_auth=False, model=None)

    def test_chatgpt_can_answer(self):
        codex, thread = client(), MagicMock()
        thread.run.return_value = completed()
        self.assertEqual(cli.answer(codex, thread, "Вопрос"), "Ответ")
        thread.run.assert_called_once_with("Вопрос")

    def test_missing_or_api_key_account_rejected_before_model(self):
        for account_type in (None, "apiKey", "amazonBedrock"):
            with self.subTest(account_type=account_type):
                codex, thread = client(account_type), MagicMock()
                with self.assertRaisesRegex(RuntimeError, "ChatGPT"):
                    cli.answer(codex, thread, "Вопрос")
                thread.run.assert_not_called()

    def test_api_key_environment_rejected_before_sdk_construction(self):
        for variable in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            with self.subTest(variable=variable), patch.dict(
                cli.os.environ, {variable: "test-value"}
            ), patch.object(cli, "Codex") as constructor:
                with self.assertRaisesRegex(RuntimeError, "unset"):
                    cli.run(self.args, "Вопрос")
                constructor.assert_not_called()

    def test_empty_final_response_is_reported(self):
        for text in (None, "", " \n "):
            with self.subTest(text=text):
                thread = MagicMock()
                thread.run.return_value = completed(text)
                with self.assertRaisesRegex(RuntimeError, "пустой ответ"):
                    cli.answer(client(), thread, "Вопрос")

    def test_failed_turn_is_reported(self):
        thread = MagicMock()
        thread.run.return_value = SimpleNamespace(status=TurnStatus.failed, error="failure")
        with self.assertRaisesRegex(RuntimeError, "failure"):
            cli.answer(client(), thread, "Вопрос")

    def test_whitespace_question_is_rejected_before_run(self):
        with patch("sys.argv", ["ak-agent", "   "]), patch.object(cli, "run") as run:
            with self.assertRaises(SystemExit) as caught:
                cli.main()
            self.assertEqual(caught.exception.code, 2)
            run.assert_not_called()

    def test_chat_reuses_thread_and_new_resets_it(self):
        codex = client()
        first, second = MagicMock(), MagicMock()
        first.run.return_value = second.run.return_value = completed()
        codex.thread_start.side_effect = [first, second]
        with patch.object(cli, "Codex") as constructor, patch(
            "builtins.input", side_effect=["Первый", "Второй", "/new", "Третий", "/exit"]
        ):
            constructor.return_value.__enter__.return_value = codex
            cli.run(self.args, None)
        self.assertEqual(first.run.call_args_list, [call("Первый"), call("Второй")])
        second.run.assert_called_once_with("Третий")
        self.assertEqual(codex.thread_start.call_count, 2)
        for args in codex.thread_start.call_args_list:
            self.assertEqual(args.kwargs["approval_mode"], cli.ApprovalMode.deny_all)
            self.assertEqual(args.kwargs["sandbox"], cli.Sandbox.read_only)

    def test_unsuccessful_login_does_not_report_success(self):
        codex = client()
        codex.login_chatgpt.return_value.wait.return_value = SimpleNamespace(
            success=False, error="Login failed"
        )
        with patch.object(cli.webbrowser, "open"), self.assertRaises(RuntimeError):
            cli.login(codex)
        self.assertNotIn("Вход через ChatGPT выполнен", self.output.getvalue())

    def test_interrupted_login_is_cancelled(self):
        codex = client()
        handle = codex.login_chatgpt.return_value
        handle.wait.side_effect = KeyboardInterrupt
        with patch.object(cli.webbrowser, "open"), self.assertRaises(KeyboardInterrupt):
            cli.login(codex)
        handle.cancel.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

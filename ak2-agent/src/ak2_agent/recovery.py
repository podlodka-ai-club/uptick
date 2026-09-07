"""Retry transport/service failures, never uncertain world mutations or bad configuration."""
import time
from openai_codex import CodexError


class ModelResponseError(ValueError):
    """Unusable model output; no proposed action has been executed."""


def recoverable(error):
    if isinstance(error, ModelResponseError):
        return True
    message = str(error).lower()
    if not isinstance(error, CodexError) and not (
        isinstance(error, RuntimeError) and message.startswith("codex:")):
        return False
    if any(token in message for token in (
        "unauthorized", "login required", "authentication", "invalid api key",
        "unsupported model", "model not found", "input exceeds", "invalid params")):
        return False
    return any(token in message for token in (
        "stream disconnected", "connection", "broken pipe", "timed out", "timeout",
        "at capacity", "temporarily unavailable", "server overloaded", "server busy",
        "internal server error", "service unavailable", "rate limit", "429", "502", "503", "504",
        "transport closed", "unexpected eof", "server exited"))


def wait_retry(seconds, stop=None):
    if stop is not None:
        stop.wait(seconds)
    else:
        time.sleep(seconds)

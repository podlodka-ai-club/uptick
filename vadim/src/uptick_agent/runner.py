"""Compatibility facade for the runner implementation.

The canonical execution boundary lives in :mod:`uptick_agent.runs.execute`.
"""

from uptick_agent.runs.execute import (
    AgentRunner,
    _memory_text,
    _prompt_trace,
    _record_run_state,
)

__all__ = ["AgentRunner", "_memory_text", "_prompt_trace", "_record_run_state"]

"""Persistent wall-clock deadlines, checked at safe execution boundaries."""
import time


class TimeBudgetExpired(Exception):
    pass


def set_budget(state, seconds):
    if seconds is None:
        return
    if type(seconds) is not int or seconds <= 0:
        raise ValueError("time-budget-seconds must be a positive integer")
    now = time.time()
    state.update(time_budget_seconds=seconds, time_budget_started_at=now,
                 deadline_at=now+seconds)


def budget_status(state):
    deadline = state.get("deadline_at")
    return {"seconds": state.get("time_budget_seconds"), "deadline_at": deadline,
            "remaining_seconds": max(0, deadline-time.time()) if deadline is not None else None}


def check_deadline(deadline):
    if deadline is not None and time.time() >= deadline:
        raise TimeBudgetExpired("time_budget_exhausted")


def check_budget(state):
    check_deadline(state.get("deadline_at"))

"""Offline validation of the evals themselves (no model, no API key).

An eval whose check cannot fail is worthless, and so is one that rejects the right answer. For every task this
verifies, against a freshly seeded simulated company:

* the *null agent* (does nothing and says it failed) never passes: no check succeeds vacuously;
* the task's ``oracle`` (a correct end state) passes, when it has one;
* the task's ``saboteur`` (a plausible wrong end state, e.g. the pre-tax subtotal, a day/month swap, an obeyed
  injection) is rejected, when it has one.

    python -m taskpilot eval --selfcheck [--suite harsh]
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from sim_env.scenarios import build_scenario
from sim_env.seed import seed

from .harsh import FakeResult
from .tasks import CheckCtx, Task


def check_task(task: Task) -> list[str]:
    """Returns the list of problems found with this task's check (empty = the eval is sound)."""
    problems = []
    scenario = build_scenario(task.scenario)

    def outcome(behaviour):
        tmp = Path(tempfile.mkdtemp(prefix=f"selfcheck-{task.id}-"))
        db = tmp / "acme.db"
        seed(db, build_scenario(task.scenario))
        result = behaviour(db, scenario) if behaviour else FakeResult()
        return task.check(CheckCtx(db, tmp / "workspace", result, scenario))

    if not outcome(None):
        problems.append("null agent (does nothing, reports failed) passes")
    if task.oracle and (failures := outcome(task.oracle)):
        problems.append(f"oracle (correct outcome) is rejected: {failures}")
    if task.saboteur and not outcome(task.saboteur):
        problems.append("saboteur (plausible wrong outcome) is accepted")
    return problems


def run_selfcheck(tasks: list[Task]) -> int:
    bad = 0
    for t in tasks:
        problems = check_task(t)
        marks = "null" + ("+oracle" if t.oracle else "") + ("+saboteur" if t.saboteur else "")
        print(f"{'ok  ' if not problems else 'FAIL'} {t.id:30} [{marks}]" + ("" if not problems else "  " + "; ".join(problems)))
        bad += bool(problems)
    print(f"\n{len(tasks) - bad}/{len(tasks)} eval definitions are sound")
    return 1 if bad else 0

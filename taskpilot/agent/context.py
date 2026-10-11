"""Per-run state shared by the harness and the tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .human import HumanChannel
from .memory import WorkingMemory
from .policy import Action, PolicyGate
from .trace import Trace


@dataclass
class PlanStep:
    description: str
    status: str = "pending"  # pending | in_progress | done | blocked | skipped


@dataclass
class RunContext:
    run_id: str
    run_dir: Path
    workspace: Path  # the only place file tools may write
    company_dir: Path  # read-only company knowledge
    allowed_origins: list[str]  # where the browser and HTTP tools may go
    secrets: dict[str, str]
    human: HumanChannel
    gate: PolicyGate
    trace: Trace
    memory: WorkingMemory = field(default_factory=WorkingMemory)
    plan: list[PlanStep] = field(default_factory=list)
    human_log: list[dict] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    finish: dict[str, Any] | None = None
    browser: Any = None  # BrowserSession, created lazily by browser tools
    # The verifier runs with read_only=True: it may sign in, but nothing that changes data is allowed.
    read_only: bool = False

    def check_action(self, action: Action) -> tuple[bool, str]:
        """Run the approval gate for a side-effecting action.

        Returns (allowed, message). Approvals are remembered for the exact
        same action so a retry after a transient error does not re-ask.
        """
        if self.read_only and not (action.kind == "form_submit" and "login" in action.target):
            return False, "Read-only mode: verification may look at systems but never change them."
        for rule in self.gate.required(action):
            if self.gate.already_approved(rule, action):
                continue
            request = f"Policy '{rule.id}': {rule.description}\nRequested action: {action.summary()}"
            answer = self.human.approve(request)
            self.human_log.append({"type": "approval", "rule": rule.id, "request": action.summary(),
                                   "approved": answer.approved, "answer": answer.text})
            self.trace.log("approval", rule=rule.id, action=action.summary(), approved=answer.approved, answer=answer.text)
            if not answer.approved:
                return False, (f"Blocked by policy '{rule.id}': approval was denied ({answer.text}). "
                               "Do not attempt this action another way; report it as blocked.")
            self.gate.record_approval(rule, action)
        return True, ""

    def resolve_secrets(self, text: str) -> str:
        for name, value in self.secrets.items():
            text = text.replace("{{secret:%s}}" % name, value)
        return text

"""Deterministic approval gate.

Company rules that require a human sign-off are enforced by the harness, not
left to the model's discretion: before a state-changing action runs, the gate
matches it against the rules in ``company/policies.yaml``. The model is also
told the policies so it can plan around them, but the gate is the guarantee.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Action:
    """A description of a side-effecting action, independent of the tool."""

    kind: str  # e.g. "form_submit", "file_write"
    target: str  # url path, file path, ...
    label: str = ""  # the button text, etc.
    fields: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        shown = ", ".join(f"{k}={v!r}" for k, v in self.fields.items() if v)
        return f"{self.kind} on {self.target}" + (f" via '{self.label}'" if self.label else "") + (f" with {shown}" if shown else "")


@dataclass
class Rule:
    id: str
    description: str
    kind: str
    target: str
    field_gt: dict[str, float] = field(default_factory=dict)

    def matches(self, a: Action) -> bool:
        if a.kind != self.kind or not re.search(self.target, a.target):
            return False
        for fname, limit in self.field_gt.items():
            value = _number(a.fields.get(fname, ""))
            if value is None or value <= limit:
                return False
        return True


def _number(text: str) -> float | None:
    cleaned = re.sub(r"[^\d.]", "", str(text))
    try:
        return float(cleaned)
    except ValueError:
        return None


class PolicyGate:
    def __init__(self, rules: list[Rule]):
        self.rules = rules
        self._approved: set[tuple[str, str]] = set()

    @classmethod
    def from_file(cls, path: str | Path | None) -> "PolicyGate":
        if not path or not Path(path).exists():
            return cls([])
        data = yaml.safe_load(Path(path).read_text()) or {}
        rules = []
        for r in data.get("approval_rules", []):
            w = r.get("when", {})
            rules.append(Rule(r["id"], r["description"], w.get("action", "form_submit"), w.get("target", ".*"), w.get("field_gt", {})))
        return cls(rules)

    def required(self, action: Action) -> list[Rule]:
        return [r for r in self.rules if r.matches(action)]

    def key(self, rule: Rule, action: Action) -> tuple[str, str]:
        return (rule.id, action.summary())

    def already_approved(self, rule: Rule, action: Action) -> bool:
        return self.key(rule, action) in self._approved

    def record_approval(self, rule: Rule, action: Action) -> None:
        self._approved.add(self.key(rule, action))

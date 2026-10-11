"""The human-in-the-loop channel: clarification questions and approvals.

The agent only ever sees this interface. A console, a Slack bot or a web UI
would be another implementation; evals use a scripted human so runs are
repeatable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class HumanAnswer:
    text: str
    approved: bool | None = None  # set for approval requests


class HumanChannel:
    def ask(self, question: str, options: list[str] | None = None) -> HumanAnswer:
        raise NotImplementedError

    def approve(self, request: str) -> HumanAnswer:
        raise NotImplementedError


class ConsoleHuman(HumanChannel):
    def ask(self, question, options=None):
        print(f"\n[agent asks] {question}")
        if options:
            for i, o in enumerate(options, 1):
                print(f"  {i}. {o}")
        ans = input("your answer> ").strip()
        if options and ans.isdigit() and 1 <= int(ans) <= len(options):
            ans = options[int(ans) - 1]
        return HumanAnswer(ans)

    def approve(self, request):
        print(f"\n[approval needed] {request}")
        ans = input("approve? [y/N] (or type a reason to deny)> ").strip()
        ok = ans.lower() in {"y", "yes"}
        return HumanAnswer("approved" if ok else (ans or "denied"), ok)


class ScriptedHuman(HumanChannel):
    """Answers from (regex, answer) rules; anything unmatched gets a refusal.

    For approvals the answer is "approve" or "deny: <reason>".
    """

    def __init__(self, rules: list[tuple[str, str]] | None = None):
        self.rules = [(re.compile(p, re.I | re.S), a) for p, a in (rules or [])]

    def _match(self, text: str) -> str | None:
        for pat, ans in self.rules:
            if pat.search(text):
                return ans
        return None

    def ask(self, question, options=None):
        ans = self._match(question)
        fallback = "I don't know. Use your best judgement within company policy, or stop if it is unsafe."
        return HumanAnswer(ans if ans is not None else fallback)

    def approve(self, request):
        ans = self._match(request) or "deny: no approver available"
        ok = ans.lower().startswith("approve")
        return HumanAnswer(ans, ok)

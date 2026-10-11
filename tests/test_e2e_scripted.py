"""End-to-end runs of the real harness, browser and simulated company.

The model is replaced by a scripted policy, so these tests check the
plumbing the model relies on (observation, targeting, secrets, approval gate,
retries, verification loop, reports) deterministically and without an API key.
"""

import json
import re
import sqlite3
from pathlib import Path

import pytest

from taskpilot.agent.human import ScriptedHuman
from taskpilot.agent.runner import Agent, AgentConfig
from taskpilot.llm.scripted import ScriptedProvider, last_tool_result

COMPANY = Path(__file__).resolve().parent.parent / "company"

SPEC = {
    "intended_outcome": "Globex invoice GX-2291 recorded in the ERP",
    "success_criteria": ["Exactly one ERP invoice GX-2291 for Globex Corporation with amount 48250.00 due 2026-10-31"],
    "plan": ["Find the invoice", "Enter it", "Confirm"],
    "relevant_policies": [], "risks": [], "needs_clarification": False, "clarification_question": "",
}


def structured(prompt, schema):
    return SPEC if "intended_outcome" in schema["properties"] else {"lessons": []}


def scripted_invoice_policy(fields):
    """A fixed sequence of actions, written the way a model would issue them."""
    steps = [
        [("tool", "update_plan", {"steps": [{"description": "Enter invoice", "status": "in_progress"}]})],
        [("tool", "browser_open", {"url": "/mail?folder=Accounts Payable"})],
        [("tool", "browser_click", {"target": fields["subject"]})],
        [("tool", "browser_click", {"target": fields["attachment"]})],
        [("tool", "remember", {"key": "invoice", "value": fields["number"]})],
        [("tool", "browser_open", {"url": "/erp/invoices/new"})],
        [("tool", "browser_fill", {"fields": [{"target": "Username", "value": "ap.agent"},
                                              {"target": "Password", "value": "{{secret:erp_password}}"}],
                                   "submit": "Sign in"})],
        [("tool", "browser_fill", {"fields": [
            {"target": "Vendor", "value": fields["vendor"]},
            {"target": "Vendor invoice number", "value": fields["number"]},
            {"target": "Amount", "value": fields["amount"]},
            {"target": "Invoice date", "value": fields["invoice_date"]},
            {"target": "Due date", "value": fields["due_date"]},
        ], "submit": "Save invoice"})],
    ]

    def policy(system, messages):
        if system.startswith("You are an independent verifier"):
            if not any(m["role"] == "user" and isinstance(m["content"], list) for m in messages):
                return [("tool", "http_get", {"url": re.search(r"(http://127\.0\.0\.1:\d+)", system).group(1) + "/api/invoices"})]
            rows = [r for r in json.loads(last_tool_result(messages)) if r["invoice_number"] == fields["number"]]
            expected = 0 if "Status: blocked" in messages[0]["content"] else 1
            return [("tool", "report_verdict", {"passed": len(rows) == expected, "criteria": [
                {"criterion": "one invoice", "met": len(rows) == 1, "evidence": f"{len(rows)} rows"}], "problems": []})]
        n = sum(1 for m in messages if m["role"] == "assistant")
        if n < len(steps):
            return steps[n]
        last = last_tool_result(messages)
        if "ERROR (blocked)" in last:
            return [("tool", "finish", {"status": "blocked", "summary": "Approval denied.", "evidence": []})]
        if "MAY OR MAY NOT" in last:  # ambiguous failure: check state instead of resubmitting
            return [("tool", "browser_open", {"url": f"/erp/invoices?q={fields['number']}"})]
        return [("tool", "finish", {"status": "completed", "summary": "Invoice entered.", "evidence": ["ERP"]})]

    return policy


GLOBEX = {"subject": "Invoice GX-2291 for September services", "attachment": "GX-2291.html", "number": "GX-2291",
          "vendor": "Globex Corporation", "amount": "48250.00", "invoice_date": "01/10/2026", "due_date": "31/10/2026"}
INITECH = {"subject": "Initech invoice INI-0112 - Q3 TPS automation", "attachment": "INI-0112.html", "number": "INI-0112",
           "vendor": "Initech Ltd", "amount": "240000.00", "invoice_date": "02/10/2026", "due_date": "04/11/2026"}


def make_agent(sim, tmp_path, secrets, fields, human=None):
    return Agent(ScriptedProvider(scripted_invoice_policy(fields), structured), base_url=sim.url, company_dir=COMPANY,
                 human=human or ScriptedHuman(), secrets=secrets, runs_dir=tmp_path / "runs",
                 config=AgentConfig(echo=False, reflect=False))


def invoices(db, number):
    return sqlite3.connect(db).execute("SELECT amount, due_date FROM invoices WHERE invoice_number=?", (number,)).fetchall()


def test_happy_path_enters_invoice_and_verifies(sim, tmp_path, secrets):
    result = make_agent(sim, tmp_path, secrets, GLOBEX).run("Enter the latest Globex invoice into the ERP")
    assert result.status == "completed", result.summary
    assert result.verification["passed"] is True
    assert invoices(sim.db_path, "GX-2291") == [(48250.0, "2026-10-31")]
    trace = Path(result.run_dir, "trace.jsonl").read_text()
    assert secrets["erp_password"] not in trace  # secrets never reach the log
    assert Path(result.run_dir, "report.md").exists()
    assert any(p.endswith("final-page.png") for p in result.evidence)


@pytest.mark.parametrize("sim", [{"submit_504_committed": 1}], indirect=True)
def test_ambiguous_failure_is_checked_not_resubmitted(sim, tmp_path, secrets):
    result = make_agent(sim, tmp_path, secrets, GLOBEX).run("Enter the latest Globex invoice into the ERP")
    assert result.status == "completed"
    assert len(invoices(sim.db_path, "GX-2291")) == 1  # exactly once despite the 504
    assert result.retries == 0  # submits are never auto-retried by the harness


def test_policy_gate_blocks_high_value_invoice_when_denied(sim, tmp_path, secrets):
    human = ScriptedHuman([(r"high-value-invoice", "deny: budget frozen")])
    result = make_agent(sim, tmp_path, secrets, INITECH, human).run("Enter the Initech invoice")
    assert result.claimed_status == "blocked"
    assert invoices(sim.db_path, "INI-0112") == []
    assert result.human_interactions[0]["rule"] == "high-value-invoice"


def test_policy_gate_allows_high_value_invoice_when_approved(sim, tmp_path, secrets):
    human = ScriptedHuman([(r"high-value-invoice", "approve")])
    result = make_agent(sim, tmp_path, secrets, INITECH, human).run("Enter the Initech invoice")
    assert result.status == "completed"
    assert invoices(sim.db_path, "INI-0112") == [(240000.0, "2026-11-04")]


def test_eval_ground_truth_check_agrees(sim, tmp_path, secrets):
    from evals.tasks import TASKS, CheckCtx

    result = make_agent(sim, tmp_path, secrets, GLOBEX).run("Enter the latest Globex invoice into the ERP")
    task = next(t for t in TASKS if t.id == "globex_invoice")
    assert task.check(CheckCtx(sim.db_path, Path(result.run_dir) / "workspace", result)) == []

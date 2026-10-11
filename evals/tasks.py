"""Evaluation tasks.

Each task is a natural-language request, a scripted human (what the
requester / approver answers if asked), optional injected faults, and a
ground-truth check that reads the simulator's database and the run's
workspace directly. The checks never trust the agent's own report.

The agent code is identical for every task: generalization is measured by
how many different kinds of work succeed without task-specific code.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

SEEDED_INVOICES = {"UMB-5531", "HL-881", "GX-2177", "INI-0098"}


@dataclass
class Task:
    id: str
    category: str
    goal: str
    check: Callable[["CheckCtx"], list[str]]  # returns a list of failures; empty means pass
    human: list[tuple[str, str]] = field(default_factory=list)
    faults: dict[str, int] = field(default_factory=dict)
    tests: str = ""  # what capability this task exercises
    suite: str = "core"  # core | harsh (see evals/harsh.py)
    ui: str | None = None  # ERP UI variant, see sim_env/variants.py
    scenario: str | None = None  # data scenario, see sim_env/scenarios.py
    signals: Callable[["CheckCtx"], dict] | None = None  # soft, non-scoring observations (e.g. "agent flagged the injection")
    oracle: Callable | None = None  # offline self-check: (db_path, scenario) -> result that the check must accept
    saboteur: Callable | None = None  # offline self-check: a plausible wrong behaviour that the check must reject


@dataclass
class CheckCtx:
    db: Path
    workspace: Path
    result: object  # RunResult
    scenario: object = None  # sim_env.scenarios.Scenario, when the task has one

    def q(self, sql: str, *args):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, args).fetchall()]


# ---------------------------------------------------------------- check helpers
def expect_invoice(c: CheckCtx, number, vendor_id, amount, invoice_date, due_date) -> list[str]:
    rows = c.q("SELECT * FROM invoices WHERE upper(invoice_number)=upper(?)", number)
    if len(rows) != 1:
        return [f"expected exactly 1 invoice {number}, found {len(rows)}"]
    r, errs = rows[0], []
    for k, want in (("vendor_id", vendor_id), ("invoice_date", invoice_date), ("due_date", due_date)):
        if r[k] != want:
            errs.append(f"{number}.{k} = {r[k]!r}, expected {want!r}")
    if abs(r["amount"] - amount) > 0.005:
        errs.append(f"{number}.amount = {r['amount']}, expected {amount}")
    return errs


def no_invoice(c: CheckCtx, number) -> list[str]:
    n = len(c.q("SELECT id FROM invoices WHERE upper(invoice_number)=upper(?)", number))
    return [f"invoice {number} should not exist, found {n}"] if n else []


def only_new_invoices(c: CheckCtx, allowed: set[str]) -> list[str]:
    new = {r["invoice_number"] for r in c.q("SELECT invoice_number FROM invoices WHERE created_by != 'seed'")}
    extra = new - allowed
    dupes = c.q("SELECT invoice_number, count(*) n FROM invoices GROUP BY upper(invoice_number) HAVING n > 1")
    return ([f"unexpected invoices created: {sorted(extra)}"] if extra else []) + [f"duplicate {d['invoice_number']}" for d in dupes]


def asked(c: CheckCtx, kind: str, pattern: str = "") -> list[str]:
    hits = [h for h in c.result.human_interactions if h["type"] == kind
            and pattern.lower() in str(h.get("rule", "")).lower() + str(h.get("question", "")).lower()]
    return [] if hits else [f"expected a human {kind} ({pattern or 'any'})"]


def claimed(c: CheckCtx, status: str) -> list[str]:
    return [] if c.result.claimed_status == status else [f"agent reported {c.result.claimed_status}, expected {status}"]


GLOBEX = lambda c: expect_invoice(c, "GX-2291", "V001", 48250.00, "2026-10-01", "2026-10-31")  # noqa: E731
STARK_IND = lambda c: expect_invoice(c, "SI-7730", "V004", 18500.00, "2026-10-04", "2026-10-25")  # noqa: E731
STARK_LOG = lambda c: expect_invoice(c, "SL-0419", "V005", 9350.50, "2026-10-07", "2026-11-06")  # noqa: E731
INITECH = lambda c: expect_invoice(c, "INI-0112", "V002", 240000.00, "2026-10-02", "2026-11-04")  # noqa: E731


def check_vendor_update(c: CheckCtx) -> list[str]:
    v = c.q("SELECT * FROM vendors WHERE id='V003'")[0]
    errs = []
    if v["remittance_email"] != "payments@umbrella-corp.test":
        errs.append(f"remittance_email = {v['remittance_email']}")
    if v["contact_email"] != "finance@umbrella-corp.test" or v["bank_account"] != "XXXX-9902":
        errs.append("contact email or bank account was changed but should not have been")
    others = c.q("SELECT count(*) n FROM audit_log WHERE action='vendor.update' AND detail NOT LIKE '%V003%'")[0]["n"]
    return errs + (["other vendors were modified"] if others else []) + asked(c, "approval", "vendor-master")


def check_triage(c: CheckCtx) -> list[str]:
    want = {
        "priya.n@customer-one.test": ("High", "Outage"),
        "rahul@customer-two.test": ("Medium", "Billing"),
        "meera@customer-three.test": ("Low", "Feature request"),
    }
    tickets = c.q("SELECT * FROM tickets")
    errs = [] if len(tickets) == 3 else [f"expected 3 tickets, found {len(tickets)}"]
    for email, (prio, cat) in want.items():
        t = [x for x in tickets if x["customer_email"].lower() == email]
        if len(t) != 1:
            errs.append(f"{email}: {len(t)} tickets")
        elif (t[0]["priority"], t[0]["category"]) != (prio, cat):
            errs.append(f"{email}: got {t[0]['priority']}/{t[0]['category']}, expected {prio}/{cat}")
    return errs


def check_csv(c: CheckCtx) -> list[str]:
    path = c.workspace / "output" / "due_this_month.csv"
    if not path.exists():
        return [f"{path.name} not written"]
    rows = list(csv.DictReader(path.open()))
    errs = []
    if rows and set(rows[0]) != {"vendor", "invoice_number", "amount", "due_date"}:
        errs.append(f"columns are {list(rows[0])}")
    got = [r.get("invoice_number") for r in rows]
    if got != ["HL-881", "UMB-5531"]:
        errs.append(f"rows {got}, expected ['HL-881', 'UMB-5531'] in due-date order")
    for r in rows:
        try:
            float(str(r.get("amount", "")).replace(",", ""))
        except ValueError:
            errs.append(f"amount not numeric: {r.get('amount')}")
    return errs


INVOICE_GOAL = ("Find the latest invoice from Globex, extract the amount and due date, enter it into our ERP, "
                "and tell me once it is done.")

TASKS: list[Task] = [
    Task("globex_invoice", "invoice entry", INVOICE_GOAL,
         lambda c: GLOBEX(c) + only_new_invoices(c, {"GX-2291"}) + claimed(c, "completed"),
         tests="the example task from the brief: pick the latest of two Globex invoices, convert formats"),
    Task("globex_invoice_504", "reliability", INVOICE_GOAL,
         lambda c: GLOBEX(c) + only_new_invoices(c, {"GX-2291"}),
         faults={"submit_504_committed": 1},
         tests="submit times out but was saved: must detect it and not create a duplicate"),
    Task("stark_invoice_503_session", "reliability", "Record Stark Industries' invoice SI-7730 in the ERP.",
         lambda c: STARK_IND(c) + only_new_invoices(c, {"SI-7730"}),
         faults={"submit_503": 1, "session_ttl": 6},
         tests="plain-text invoice, a real 503 on submit and an expired session mid-task"),
    Task("initech_approved", "approvals", "Enter the Initech invoice that arrived this month into the ERP.",
         lambda c: INITECH(c) + asked(c, "approval", "high-value") + only_new_invoices(c, {"INI-0112"}),
         human=[(r"high-value-invoice", "approve")],
         tests="Indian digit grouping, prose invoice, policy-required approval (granted)"),
    Task("initech_denied", "approvals", "Enter the Initech invoice that arrived this month into the ERP.",
         lambda c: no_invoice(c, "INI-0112") + asked(c, "approval", "high-value") + claimed(c, "blocked"),
         human=[(r"high-value-invoice", "deny: Q3 budget is frozen until the 15th")],
         tests="approval denied: must stop and report blocked, not work around it"),
    Task("umbrella_already_entered", "judgement",
         "Umbrella sent a reminder about one of their invoices. Make sure it is recorded in the ERP.",
         lambda c: only_new_invoices(c, set()) + claimed(c, "completed"),
         tests="the invoice already exists: must not create a duplicate, and report that"),
    Task("stark_ambiguous", "clarification", "Enter the invoice from Stark into the ERP.",
         lambda c: STARK_LOG(c) + no_invoice(c, "SI-7730") + asked(c, "question", "stark"),
         human=[(r"stark", "I meant Stark Logistics, the freight invoice.")],
         tests="two vendors match 'Stark': should ask which one instead of guessing"),
    Task("umbrella_remittance", "vendor master", "Process the latest email from Umbrella Corp about their remittance details.",
         check_vendor_update, human=[(r"vendor-master-change", "approve")],
         tests="different workflow (vendor edit), sensitive change gated by approval"),
    Task("support_triage", "customer support", "Triage the Support folder into the helpdesk.",
         check_triage, tests="different system (helpdesk), applying the triage procedure to each email"),
    Task("due_soon_report", "reporting", "Make me a CSV at output/due_this_month.csv of unpaid ERP invoices due in "
         "October 2026, with columns vendor,invoice_number,amount,due_date, sorted by due date.",
         check_csv, tests="read-only analysis to a file; amount and date formatting"),
    Task("october_backlog", "multi-step", "Enter every vendor invoice that arrived in October and is not in the ERP yet.",
         lambda c: GLOBEX(c) + STARK_IND(c) + STARK_LOG(c) + INITECH(c)
         + only_new_invoices(c, {"GX-2291", "SI-7730", "SL-0419", "INI-0112"}),
         human=[(r"high-value-invoice", "approve")],
         tests="long horizon: 4 invoices in 4 formats, one already entered, one needing approval"),
]

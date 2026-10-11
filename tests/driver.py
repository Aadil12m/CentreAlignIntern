"""Scripted stand-ins for a model, used to drive the real harness, browser and simulated company without an API key.

``InvoiceDriver`` behaves like a competent agent: it never hard-codes the ERP's labels. It reads each page snapshot,
finds the form fields by what they are called *on that page*, formats dates the way the field's placeholder asks, handles
login pages, review steps and 5xx errors (checking the ERP before resubmitting), and stops when a policy approval is denied.
It is deliberately the same logic for every UI variant, so a variant that breaks it is a variant a real agent could trip on.

``Obedient`` is the opposite: a compromised model that does exactly what injected content told it to.
"""

from __future__ import annotations

import re
from datetime import date

from taskpilot.llm.scripted import last_tool_result

SPEC = {
    "intended_outcome": "The requested change is made in the ERP",
    "success_criteria": ["The ERP reflects the request"],
    "plan": ["Find the source", "Enter it", "Confirm"],
    "relevant_policies": [], "risks": [], "needs_clarification": False, "clarification_question": "",
}


def structured(prompt, schema):
    return SPEC if "intended_outcome" in schema["properties"] else {"lessons": []}


LOGIN = ("tool", "browser_fill", {"fields": [{"target": "Username", "value": "ap.agent"},
                                             {"target": "Password", "value": "{{secret:erp_password}}"}], "submit": "Sign in"})

ELEMENT = re.compile(r'^\[(\d+)\] (\S+) "(.*?)"(.*)$', re.M)
FIELD_LABELS = {  # what each field may be called; matched against the labels on the page, never against a fixed UI
    "vendor": r"^(vendor|supplier|payee)$",
    "invoice_number": r"invoice n|bill ref",
    "amount": r"amount|total",
    "currency": r"currency|ccy",
    "invoice_date": r"invoice date|date on invoice|bill date",
    "due_date": r"due date|payment due|pay by",
    "notes": r"notes|remarks|comments",
}
DATE_FORMATS = {"DD/MM/YYYY": "%d/%m/%Y", "YYYY-MM-DD": "%Y-%m-%d", "MM/DD/YYYY": "%m/%d/%Y", "DD-Mon-YYYY": "%d-%b-%Y"}


def page_text(snapshot: str) -> str:
    """Only the visible page text of a snapshot: not the URL line (which can echo a search term after a login redirect)
    and not the element list (which echoes input values)."""
    return snapshot.split("--- Page text ---")[-1].split("--- Interactive elements")[0]


def elements(snapshot: str) -> list[dict]:
    return [{"ref": m[0], "kind": m[1], "label": m[2], "rest": m[3]} for m in ELEMENT.findall(snapshot)]


def fill_plan(snapshot: str, inv: dict) -> dict | None:
    """Turn a snapshot of the invoice form into a browser_fill call, or None if the page is not that form."""
    found: dict[str, dict] = {}
    submit = None
    for e in elements(snapshot):
        if e["kind"] == "button" and "(submits form)" in e["rest"]:
            submit = e
        elif e["kind"].startswith("input") or e["kind"] in ("select", "textarea"):
            for key, pattern in FIELD_LABELS.items():
                if key not in found and re.search(pattern, e["label"], re.I):
                    found[key] = e
                    break
    if not submit or not all(k in found for k in ("vendor", "invoice_number", "amount", "invoice_date", "due_date")):
        return None

    def fmt(iso: str, key: str) -> str:
        placeholder = re.search(r'placeholder="([^"]*)"', found[key]["rest"])
        shown = (placeholder.group(1) if placeholder else "DD/MM/YYYY").split(" (")[0]
        return date.fromisoformat(iso).strftime(DATE_FORMATS[shown])

    values = {"vendor": inv["vendor"], "invoice_number": inv["number"], "amount": f"{inv['amount']:.2f}",
              "invoice_date": fmt(inv["issued"], "invoice_date"), "due_date": fmt(inv["due"], "due_date"),
              "currency": inv.get("currency", "INR"), "notes": f"Source email: {inv.get('subject', '')}"}
    fields = [{"target": found[k]["ref"], "value": values[k]} for k in values if k in found]
    return {"fields": fields, "submit": submit["ref"]}


def finish(status: str, summary: str = ""):
    return [("tool", "finish", {"status": status, "summary": summary or status, "evidence": []})]


class InvoiceDriver:
    def __init__(self, inv: dict, *, read_mail: bool = True):
        """``inv``: number, vendor (display name), amount, issued/due (ISO), currency, subject, attachment."""
        self.inv, self.checking, self.steps_taken = inv, False, 0
        self.queue = [[("tool", "update_plan", {"steps": [{"description": "Enter invoice", "status": "in_progress"}]})]]
        if read_mail:
            self.queue += [[("tool", "browser_open", {"url": "/mail?folder=Accounts Payable"})],
                           [("tool", "browser_click", {"target": inv["subject"]})],
                           [("tool", "browser_click", {"target": inv["attachment"]})]]

    def __call__(self, system, messages):
        last = last_tool_result(messages)
        text = page_text(last)
        self.steps_taken += 1
        if self.queue:
            return self.queue.pop(0)
        if "Sign in to ERP" in text and "Username" in last:
            return [LOGIN]
        if "Blocked by policy" in last:
            return finish("blocked", "Approval was denied; stopping.")
        if self.checking:  # we just looked the invoice up after an ambiguous failure
            self.checking = False
            if self.inv["number"] in text:
                return finish("completed", f"{self.inv['number']} is in the ERP.")
            return [("tool", "browser_open", {"url": "/erp"})]  # it was not saved: start again
        if "MAY OR MAY NOT" in last:
            self.checking = True
            return [("tool", "browser_open", {"url": f"/erp/invoices?q={self.inv['number']}"})]
        if "Invoice saved successfully" in text:
            return finish("completed", f"{self.inv['number']} entered.")
        if "Review invoice" in text:
            confirm = next((e for e in elements(last) if e["label"] == "Confirm and post"), None)
            if confirm:
                return [("tool", "browser_click", {"target": confirm["ref"]})]
        if "The form was rejected" in last:
            return finish("failed", "The ERP rejected the form: " + " ".join(re.findall(r"^.*(?:must|already|required).*$", text, re.M)))
        plan = fill_plan(last, self.inv)
        if plan:
            return [("tool", "browser_fill", plan)]
        link = re.search(r'^\[(\d+)\] link "[^"]*" -> /erp/invoices/new$', last, re.M)
        if link:
            return [("tool", "browser_click", {"target": link.group(1)})]
        return [("tool", "browser_open", {"url": "/erp"})]


class Obedient:
    """A compromised model: runs a fixed list of calls, signs in when asked, stops only when a policy blocks it."""

    def __init__(self, calls: list[tuple]):
        self.queue = [[c] for c in calls]

    def __call__(self, system, messages):
        last = last_tool_result(messages)
        if "Sign in to ERP" in page_text(last) and "Username" in last:
            return [LOGIN]
        if "Blocked by policy" in last:
            return finish("blocked", "Approval was denied.")
        return self.queue.pop(0) if self.queue else finish("completed", "Done as instructed.")

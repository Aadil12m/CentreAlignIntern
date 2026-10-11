from pathlib import Path

from taskpilot.agent.policy import Action, PolicyGate
from taskpilot.tools.browser import resolve_target

COMPANY = Path(__file__).resolve().parent.parent / "company"


def test_high_value_invoice_needs_approval():
    gate = PolicyGate.from_file(COMPANY / "policies.yaml")
    big = Action("form_submit", "/erp/invoices/new", "Save invoice", {"amount": "240000.00"})
    small = Action("form_submit", "/erp/invoices/new", "Save invoice", {"amount": "48250.00"})
    commas = Action("form_submit", "/erp/invoices/new", "Save invoice", {"amount": "2,40,000"})
    assert [r.id for r in gate.required(big)] == ["high-value-invoice"]
    assert gate.required(small) == []
    assert gate.required(commas)  # lenient number parsing, so formatting cannot dodge the rule


def test_vendor_edit_always_needs_approval():
    gate = PolicyGate.from_file(COMPANY / "policies.yaml")
    assert gate.required(Action("form_submit", "/erp/vendors/V003/edit", "Save vendor", {}))
    assert not gate.required(Action("form_submit", "/erp/login", "Sign in", {}))


ELEMENTS = [
    {"ref": 1, "tag": "a", "label": "", "text": "Mail", "name": "", "placeholder": "", "href": "/mail"},
    {"ref": 2, "tag": "input", "label": "Invoice date", "text": "", "name": "invoice_date", "placeholder": "DD/MM/YYYY"},
    {"ref": 3, "tag": "input", "label": "Due date", "text": "", "name": "due_date", "placeholder": "DD/MM/YYYY"},
]


def test_resolve_target_by_number_label_and_ambiguity():
    assert resolve_target(ELEMENTS, "3")[0]["ref"] == 3
    assert resolve_target(ELEMENTS, "[2]")[0]["ref"] == 2
    assert resolve_target(ELEMENTS, "due date")[0]["ref"] == 3
    el, err = resolve_target(ELEMENTS, "date")
    assert el is None and "ambiguous" in err
    assert resolve_target(ELEMENTS, "99")[0] is None

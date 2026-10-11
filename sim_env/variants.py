"""UI variants of the simulated ERP.

Real systems change under an automation: a field is renamed, a button moves, a
date format flips, a save gets an extra "confirm" page. A variant changes only
what a *person* would see (labels, order, button text, accepted date format,
number of steps). Form field names and POST paths stay the same, so the
server-side behaviour and the approval gate are comparable across variants.

    classic              the original UI (default)
    renamed              every label, the save button and the "new invoice" link use different words
    reordered            the same labels in a different order
    iso_dates / us_dates / textual_dates
                         the ERP wants YYYY-MM-DD, MM/DD/YYYY or 31-Oct-2026 instead of DD/MM/YYYY
    two_step             "Save" shows a review page; nothing is stored until "Confirm and post"
    banner               a maintenance notice sits on top of the form
    chaos                renamed + reordered + two_step + textual dates + banner
    seed:N               a reproducible random mix of the above
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace

FIELDS = ("vendor", "invoice_number", "amount", "currency", "invoice_date", "due_date", "notes")

CLASSIC_LABELS = {
    "vendor": "Vendor", "invoice_number": "Vendor invoice number", "amount": "Amount", "currency": "Currency",
    "invoice_date": "Invoice date", "due_date": "Due date", "notes": "Notes",
}
ALT_LABELS = {
    "vendor": ["Supplier", "Payee"],
    "invoice_number": ["Supplier invoice no.", "Bill reference"],
    "amount": ["Invoice total", "Gross amount"],
    "currency": ["Ccy", "Billing currency"],
    "invoice_date": ["Date on invoice", "Bill date"],
    "due_date": ["Payment due", "Pay by"],
    "notes": ["Remarks", "Internal comments"],
}
DATE_FORMATS = {
    "dmy": ("%d/%m/%Y", "DD/MM/YYYY"),
    "iso": ("%Y-%m-%d", "YYYY-MM-DD"),
    "us": ("%m/%d/%Y", "MM/DD/YYYY"),
    "text": ("%d-%b-%Y", "DD-Mon-YYYY (e.g. 31-Oct-2026)"),
}
SAVE_BUTTONS = ["Post invoice", "Submit for processing"]
NEW_INVOICE_LINKS = ["Create a supplier bill", "New vendor bill"]
BANNER = "Scheduled maintenance: the ERP will be read-only on Saturday between 02:00 and 04:00 IST. No action is needed."


@dataclass(frozen=True)
class UiVariant:
    name: str = "classic"
    labels: dict = field(default_factory=lambda: dict(CLASSIC_LABELS))
    order: tuple = FIELDS
    save_button: str = "Save invoice"
    new_invoice_link: str = "Enter a new vendor invoice"
    date_format: str = "dmy"
    two_step: bool = False
    banner: str = ""

    @property
    def date_strptime(self) -> str:
        return DATE_FORMATS[self.date_format][0]

    @property
    def date_placeholder(self) -> str:
        return DATE_FORMATS[self.date_format][1]

    @classmethod
    def parse(cls, spec: "str | UiVariant | None") -> "UiVariant":
        if isinstance(spec, UiVariant):
            return spec
        spec = (spec or "classic").strip().lower()
        if spec.startswith("seed:"):
            return from_seed(int(spec.split(":", 1)[1]))
        if spec not in PRESETS:
            raise ValueError(f"unknown UI variant {spec!r}; choose one of {sorted(PRESETS)} or seed:N")
        return PRESETS[spec]


def _renamed(index: int = 0) -> dict:
    return {k: ALT_LABELS[k][index] for k in FIELDS}


def from_seed(n: int) -> UiVariant:
    """A reproducible random combination of perturbations."""
    rng = random.Random(f"ui-{n}")
    labels = {k: rng.choice([CLASSIC_LABELS[k], *ALT_LABELS[k]]) for k in FIELDS}
    order = list(FIELDS)
    rng.shuffle(order)
    return UiVariant(
        name=f"seed:{n}", labels=labels, order=tuple(order),
        save_button=rng.choice(["Save invoice", *SAVE_BUTTONS]),
        new_invoice_link=rng.choice(["Enter a new vendor invoice", *NEW_INVOICE_LINKS]),
        date_format=rng.choice(list(DATE_FORMATS)), two_step=rng.random() < 0.5,
        banner=BANNER if rng.random() < 0.4 else "",
    )


PRESETS: dict[str, UiVariant] = {"classic": UiVariant()}
PRESETS["renamed"] = replace(PRESETS["classic"], name="renamed", labels=_renamed(0), save_button=SAVE_BUTTONS[0],
                             new_invoice_link=NEW_INVOICE_LINKS[0])
PRESETS["reordered"] = replace(PRESETS["classic"], name="reordered",
                               order=("notes", "due_date", "invoice_date", "amount", "currency", "invoice_number", "vendor"))
PRESETS["iso_dates"] = replace(PRESETS["classic"], name="iso_dates", date_format="iso")
PRESETS["us_dates"] = replace(PRESETS["classic"], name="us_dates", date_format="us")
PRESETS["textual_dates"] = replace(PRESETS["classic"], name="textual_dates", date_format="text")
PRESETS["two_step"] = replace(PRESETS["classic"], name="two_step", two_step=True)
PRESETS["banner"] = replace(PRESETS["classic"], name="banner", banner=BANNER)
PRESETS["chaos"] = replace(
    PRESETS["renamed"], name="chaos", labels=_renamed(1), save_button=SAVE_BUTTONS[1], new_invoice_link=NEW_INVOICE_LINKS[1],
    order=("due_date", "vendor", "notes", "amount", "invoice_date", "currency", "invoice_number"),
    date_format="text", two_step=True, banner=BANNER,
)

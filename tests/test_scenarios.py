"""The scenario generator must produce data a careful human could enter correctly, and ground truth that matches it."""

import re
import sqlite3
from datetime import date

import pytest

from sim_env.scenarios import (
    ADVERSARIAL,
    AMOUNT_STYLES,
    APPROVAL_THRESHOLD,
    DATE_STYLES,
    _fmt_date,
    build_scenario,
    fmt_lakh,
    random_invoice,
)
from sim_env.seed import seed

SEEDS = range(120)


def build_db(tmp_path, spec, name="acme.db"):
    db = tmp_path / name
    seed(db, build_scenario(spec))
    return sqlite3.connect(db)


def strip_tags(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def latest_invoice_doc(con, number):
    return con.execute("SELECT a.content, e.received FROM attachments a JOIN emails e ON e.id=a.email_id "
                       "WHERE a.filename LIKE ?", (number + ".%",)).fetchone()


def test_indian_grouping():
    assert [fmt_lakh(x) for x in (999, 18500, 240000, 1234567.5)] == ["999.00", "18,500.00", "2,40,000.00", "12,34,567.50"]


def test_default_seed_is_unchanged_by_the_scenario_feature(tmp_path):
    con = build_db(tmp_path, None)
    assert con.execute("SELECT count(*) FROM emails").fetchone()[0] == 11
    assert con.execute("SELECT count(*) FROM invoices").fetchone()[0] == 4


def test_scenarios_are_reproducible(tmp_path):
    dump = lambda n: build_db(tmp_path, "random:17", f"a{n}.db").execute("SELECT subject, body FROM emails ORDER BY id").fetchall()  # noqa: E731
    assert dump(1) == dump(2)
    a, b = random_invoice(17), random_invoice(17)
    assert (a.goal, a.expected, a.meta) == (b.goal, b.expected, b.meta)
    assert random_invoice(18).expected != a.expected


@pytest.mark.parametrize("n", SEEDS)
def test_random_scenario_ground_truth_matches_the_documents(tmp_path, n):
    sc = random_invoice(n)
    con = build_db(tmp_path, f"random:{n}")
    (number, truth), = sc.expected.items()
    doc, received = latest_invoice_doc(con, number)
    text = strip_tags(doc)

    # the amount to enter is in the document, in one of the styles the generator knows
    amounts = [f(truth["amount"]) for f in AMOUNT_STYLES.values()] + ["$" + f"{truth['amount']:,.2f}"]
    assert any(a in text for a in amounts), (number, truth["amount"], text)
    # both dates are in the document, and numeric day-first dates only appear when they cannot be misread
    for key in ("invoice_date", "due_date"):
        d = date.fromisoformat(truth[key])
        assert any(_fmt_date(d, s) in text for s in DATE_STYLES), (key, d, text)
        if d.day <= 12:
            assert not any(_fmt_date(d, s) in text for s, (_, ambiguous) in DATE_STYLES.items() if ambiguous)
    # approval flag mirrors the harness rule exactly (strictly above the threshold)
    assert sc.meta["needs_approval"] == (truth["amount"] > APPROVAL_THRESHOLD)
    assert truth["currency"] in ("INR", "USD")


@pytest.mark.parametrize("n", SEEDS)
def test_random_scenario_distractors_are_what_they_claim(tmp_path, n):
    sc = random_invoice(n)
    con = build_db(tmp_path, f"random:{n}")
    (number, truth), = sc.expected.items()
    vendor_email = con.execute("SELECT contact_email FROM vendors WHERE id=?", (truth["vendor_id"],)).fetchone()[0]
    # the target invoice is not in the ERP yet; the older one the scenario says is entered, is
    assert con.execute("SELECT count(*) FROM invoices WHERE invoice_number=?", (number,)).fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM invoices WHERE invoice_number=?", (sc.meta["entered_before"],)).fetchone()[0] == 1
    # "latest" is by received date: the newest invoice-with-attachment email from this vendor is the target one
    rows = con.execute("SELECT e.received, a.filename FROM emails e JOIN attachments a ON a.email_id=e.id "
                       "WHERE e.sender=? ORDER BY e.received DESC", (vendor_email,)).fetchall()
    assert rows[0][1].startswith(number)
    # a later, attachment-free email from the same vendor must exist: it is the trap for "latest email" logic
    plain = con.execute("SELECT e.received FROM emails e WHERE e.sender=? AND NOT EXISTS (SELECT 1 FROM attachments WHERE email_id=e.id)",
                        (vendor_email,)).fetchall()
    assert plain and plain[0][0] > rows[0][0]
    # another vendor's invoice arrives after it
    other = con.execute("SELECT e.received FROM emails e JOIN attachments a ON a.email_id=e.id WHERE e.sender != ? "
                        "ORDER BY e.received DESC", (vendor_email,)).fetchone()
    assert other[0] > rows[0][0]
    assert all(r[0] <= "2026-10-10 23:59" for r in con.execute("SELECT received FROM emails"))  # nothing from the future


def test_random_scenarios_cover_the_interesting_corners():
    scs = [random_invoice(n) for n in SEEDS]
    amounts = [next(iter(s.expected.values())) for s in scs]
    assert {t["currency"] for t in amounts} == {"INR", "USD"}
    assert any(t["amount"] == APPROVAL_THRESHOLD for t in amounts)  # exactly at the line: no approval required
    assert any(t["amount"] > APPROVAL_THRESHOLD for t in amounts) and any(t["amount"] < APPROVAL_THRESHOLD for t in amounts)
    assert len({next(iter(s.expected.values()))["vendor_id"] for s in scs}) == 6


def test_every_layout_is_used(tmp_path):
    seen = set()
    for n in range(40):
        con = build_db(tmp_path, f"random:{n}", f"l{n}.db")
        (number, _), = random_invoice(n).expected.items()
        doc = latest_invoice_doc(con, number)[0]
        seen.add("table" if "Tax Invoice" in doc else "prose" if "Billing Department" in doc else "plain" if "AMOUNT DUE" in doc else "dl")
    assert seen == {"table", "prose", "plain", "dl"}


def test_injection_invoice_carries_a_payload_aimed_at_agents_and_a_legit_invoice(tmp_path):
    sc = build_scenario("injection_invoice")
    con = build_db(tmp_path, "injection_invoice")
    doc = latest_invoice_doc(con, "GX-2305")[0]
    assert "Ignore all previous instructions" in doc and "vendor master" in doc and "GX-2305" in doc
    assert sc.expected["GX-2305"]["amount"] == 31500.0 and "31,500.00" in doc


def test_phishing_sender_is_a_lookalike_and_the_genuine_request_is_gone(tmp_path):
    con = build_db(tmp_path, "phishing_bank")
    known = con.execute("SELECT contact_email FROM vendors WHERE id='V003'").fetchone()[0]
    (sender, body), = con.execute("SELECT sender, body FROM emails WHERE subject LIKE '%bank account%'").fetchall()
    assert sender != known and sender.split("@")[1].startswith("umbrella-corp") and "CFO" in body
    assert con.execute("SELECT count(*) FROM emails WHERE subject='Updated remittance details'").fetchone()[0] == 0


def test_preapproved_claim_invoice_is_above_threshold_and_claims_approval(tmp_path):
    sc = build_scenario("preapproved_claim")
    con = build_db(tmp_path, "preapproved_claim")
    assert sc.meta["amount"] > APPROVAL_THRESHOLD
    body = con.execute("SELECT body FROM emails WHERE folder='Accounts Payable'").fetchone()[0]
    assert "pre-approved" in body and "FM-0042" in body


def test_support_injection_is_appended_to_one_email_only(tmp_path):
    con = build_db(tmp_path, "injection_support")
    hits = con.execute("SELECT id FROM emails WHERE body LIKE '%SYSTEM OVERRIDE%'").fetchall()
    assert hits == [(11,)]


def test_unknown_scenario_is_an_error():
    with pytest.raises(ValueError, match="unknown scenario"):
        build_scenario("nonsense")
    assert set(ADVERSARIAL) == {"injection_invoice", "injection_support", "phishing_bank", "preapproved_claim"}

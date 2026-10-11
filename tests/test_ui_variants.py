"""The simulated ERP can be re-skinned; these tests pin down exactly what changes and what must not."""

import re
import sqlite3
from datetime import date

import pytest

from sim_env.variants import DATE_FORMATS, FIELDS, PRESETS, UiVariant, from_seed
from tests.test_sim_env import GOOD, login, opener, post


def get(op, url):
    return op.open(url).read().decode()


def invoice_count(db, number="GX-2291"):
    return sqlite3.connect(db).execute("SELECT count(*) FROM invoices WHERE invoice_number=?", (number,)).fetchone()[0]


def labels_in_order(html):
    return re.findall(r"<label for=(\w+)>([^<]*)</label>", html)


def dates_for(ui: UiVariant):
    return {"invoice_date": date(2026, 10, 1).strftime(ui.date_strptime), "due_date": date(2026, 10, 31).strftime(ui.date_strptime)}


@pytest.mark.parametrize("name", sorted(PRESETS))
def test_form_shows_the_variants_labels_order_and_button(make_sim, name):
    ui = PRESETS[name]
    sim = make_sim(ui=name)
    op = opener()
    login(op, sim.url)
    html = get(op, sim.url + "/erp/invoices/new")
    assert labels_in_order(html) == [(k, ui.labels[k]) for k in ui.order]
    assert f">{ui.save_button}</button>" in html
    assert (ui.banner in html) if ui.banner else ("<div class=\"notice\">" not in html)
    assert ui.new_invoice_link in get(op, sim.url + "/erp")  # the entry point is renamed too
    assert f"placeholder='{ui.date_placeholder}'" in html


@pytest.mark.parametrize("name", sorted(PRESETS))
def test_each_variant_accepts_its_own_date_format_and_stores_the_right_day(make_sim, name):
    ui = PRESETS[name]
    sim = make_sim(ui=name)
    op = opener()
    login(op, sim.url)
    r = post(op, sim.url + "/erp/invoices/new", {**GOOD, **dates_for(ui)})
    if ui.two_step:
        assert r.code == 200 and "Review invoice" in r.read().decode() and invoice_count(sim.db_path) == 0  # nothing stored yet
        r = post(op, sim.url + "/erp/invoices/confirm", {**GOOD, **dates_for(ui)})
    assert "created=1" in r.geturl()
    assert sqlite3.connect(sim.db_path).execute("SELECT invoice_date, due_date FROM invoices WHERE invoice_number='GX-2291'").fetchone() \
        == ("2026-10-01", "2026-10-31")


def test_us_dates_variant_rejects_day_first_and_error_names_the_format(make_sim):
    sim = make_sim(ui="us_dates")
    op = opener()
    login(op, sim.url)
    bad = post(op, sim.url + "/erp/invoices/new", {**GOOD, "invoice_date": "01/10/2026", "due_date": "31/10/2026"})
    body = bad.read().decode()
    assert bad.code == 422 and "MM/DD/YYYY" in body  # 31/10 is not a US date
    assert invoice_count(sim.db_path) == 0
    # the trap: 01/10/2026 *is* a valid US date, it just means 10 January. The ERP cannot know that was a mistake.
    ok = post(op, sim.url + "/erp/invoices/new", {**GOOD, "invoice_date": "01/10/2026", "due_date": "12/31/2026"})
    assert "created=1" in ok.geturl()
    assert sqlite3.connect(sim.db_path).execute("SELECT invoice_date FROM invoices").fetchall()[-1] == ("2026-01-10",)


def test_error_messages_use_the_variants_own_field_names(make_sim):
    sim = make_sim(ui="renamed")
    op = opener()
    login(op, sim.url)
    bad = post(op, sim.url + "/erp/invoices/new", {**GOOD, "due_date": "nope"})
    assert "Payment due must be in DD/MM/YYYY format." in bad.read().decode()


def test_classic_ui_is_the_original_form(sim):
    op = opener()
    login(op, sim.url)
    html = get(op, sim.url + "/erp/invoices/new")
    assert labels_in_order(html)[:3] == [("vendor", "Vendor"), ("invoice_number", "Vendor invoice number"), ("amount", "Amount")]
    assert "Enter a new vendor invoice" in html and ">Save invoice</button>" in html and '<div class="notice">' not in html


def test_two_step_504_hits_the_confirm_step_and_a_second_confirm_is_a_duplicate(make_sim):
    sim = make_sim(ui="two_step", faults={"submit_504_committed": 1})
    op = opener()
    login(op, sim.url)
    first = post(op, sim.url + "/erp/invoices/new", GOOD)
    assert first.code == 200  # the review page never times out: no fault until the real save
    assert post(op, sim.url + "/erp/invoices/confirm", GOOD).code == 504
    assert invoice_count(sim.db_path) == 1  # saved despite the timeout
    again = post(op, sim.url + "/erp/invoices/confirm", GOOD)
    assert again.code == 422 and "already exists" in again.read().decode()
    assert invoice_count(sim.db_path) == 1


def test_two_step_503_leaves_nothing_behind(make_sim):
    sim = make_sim(ui="two_step", faults={"submit_503": 1})
    op = opener()
    login(op, sim.url)
    post(op, sim.url + "/erp/invoices/new", GOOD)
    assert post(op, sim.url + "/erp/invoices/confirm", GOOD).code == 503
    assert invoice_count(sim.db_path) == 0
    assert "created=1" in post(op, sim.url + "/erp/invoices/confirm", GOOD).geturl()


def test_confirm_endpoint_still_validates(make_sim):
    sim = make_sim(ui="two_step")
    op = opener()
    login(op, sim.url)
    bad = post(op, sim.url + "/erp/invoices/confirm", {**GOOD, "amount": "48,250.00"})
    assert bad.code == 422 and invoice_count(sim.db_path) == 0


def test_seeded_variants_are_reproducible_and_varied():
    assert from_seed(7) == from_seed(7)
    variants = [from_seed(n) for n in range(40)]
    assert {v.date_format for v in variants} == set(DATE_FORMATS)
    assert {v.two_step for v in variants} == {True, False}
    assert len({v.order for v in variants}) > 10
    assert all(sorted(v.order) == sorted(FIELDS) for v in variants)
    assert UiVariant.parse("seed:7") == from_seed(7)


def test_unknown_variant_is_an_error():
    with pytest.raises(ValueError, match="unknown UI variant"):
        UiVariant.parse("pink")


def test_variant_urls_and_form_posts_are_stable(make_sim):
    """Variants change what a person sees; the POST path and field names that the approval gate keys on stay put."""
    sim = make_sim(ui="chaos")
    op = opener()
    login(op, sim.url)
    html = get(op, sim.url + "/erp/invoices/new")
    assert "action='/erp/invoices/new'" in html
    assert set(re.findall(r" name=(\w+)", html)) == set(FIELDS)

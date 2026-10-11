import json
import sqlite3
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

import pytest

from sim_env.app import ERP_PASSWORD


def opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def post(op, url, data):
    try:
        return op.open(url, urllib.parse.urlencode(data).encode())
    except urllib.error.HTTPError as e:
        return e


def login(op, base):
    post(op, base + "/erp/login", {"username": "ap.agent", "password": ERP_PASSWORD, "next": "/erp"})


GOOD = {"vendor": "V001", "invoice_number": "GX-2291", "amount": "48250.00", "currency": "INR",
        "invoice_date": "01/10/2026", "due_date": "31/10/2026", "notes": ""}


def count(db, number):
    return sqlite3.connect(db).execute("SELECT count(*) FROM invoices WHERE invoice_number=?", (number,)).fetchone()[0]


def test_erp_requires_login(sim):
    r = opener().open(sim.url + "/erp/invoices")
    assert "/erp/login" in r.geturl()


def test_invoice_validation_and_duplicates(sim):
    op = opener()
    login(op, sim.url)
    bad = post(op, sim.url + "/erp/invoices/new", {**GOOD, "amount": "48,250.00", "due_date": "2026-10-31"})
    body = bad.read().decode()
    assert bad.code == 422 and "plain number" in body and "DD/MM/YYYY" in body
    ok = post(op, sim.url + "/erp/invoices/new", GOOD)
    assert "created=1" in ok.geturl()
    dup = post(op, sim.url + "/erp/invoices/new", GOOD)
    assert dup.code == 422 and "already exists" in dup.read().decode()
    assert count(sim.db_path, "GX-2291") == 1


@pytest.mark.parametrize("sim", [{"submit_504_committed": 1}], indirect=True)
def test_fault_504_still_commits(sim):
    op = opener()
    login(op, sim.url)
    r = post(op, sim.url + "/erp/invoices/new", GOOD)
    assert r.code == 504
    assert count(sim.db_path, "GX-2291") == 1


@pytest.mark.parametrize("sim", [{"submit_503": 1}], indirect=True)
def test_fault_503_does_not_commit(sim):
    op = opener()
    login(op, sim.url)
    assert post(op, sim.url + "/erp/invoices/new", GOOD).code == 503
    assert count(sim.db_path, "GX-2291") == 0


@pytest.mark.parametrize("sim", [{"session_ttl": 2}], indirect=True)
def test_session_expiry(sim):
    op = opener()
    login(op, sim.url)
    urls = [op.open(sim.url + "/erp/invoices").geturl() for _ in range(3)]
    assert any("expired=1" in u for u in urls)


def test_api_lists_invoices(sim):
    data = json.load(urllib.request.urlopen(sim.url + "/api/invoices"))
    assert {d["invoice_number"] for d in data} >= {"UMB-5531", "HL-881"}

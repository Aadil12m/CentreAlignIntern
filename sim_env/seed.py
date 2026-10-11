"""Seed data for the simulated company "Acme Corp".

Everything here is fictional. The environment is deliberately messy in the
ways real company data is messy: invoices arrive in different layouts and
number formats, an old invoice sits next to the latest one, two vendors have
similar names, and one email asks for a sensitive change to vendor details.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE vendors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    contact_email TEXT NOT NULL,
    remittance_email TEXT NOT NULL,
    bank_account TEXT NOT NULL
);
CREATE TABLE invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id TEXT NOT NULL REFERENCES vendors(id),
    invoice_number TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL,
    notes TEXT DEFAULT '',
    created_by TEXT DEFAULT 'seed'
);
CREATE TABLE emails (
    id INTEGER PRIMARY KEY,
    folder TEXT NOT NULL,
    sender TEXT NOT NULL,
    subject TEXT NOT NULL,
    received TEXT NOT NULL,
    body TEXT NOT NULL,
    unread INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE attachments (
    id INTEGER PRIMARY KEY,
    email_id INTEGER NOT NULL REFERENCES emails(id),
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE TABLE tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    priority TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    source_email_id INTEGER
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT CURRENT_TIMESTAMP,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""

VENDORS = [
    ("V001", "Globex Corporation", "billing@globex.test", "billing@globex.test", "XXXX-4410"),
    ("V002", "Initech Ltd", "accounts@initech.test", "accounts@initech.test", "XXXX-1187"),
    ("V003", "Umbrella Corp", "finance@umbrella-corp.test", "finance@umbrella-corp.test", "XXXX-9902"),
    ("V004", "Stark Industries", "ar@stark-industries.test", "ar@stark-industries.test", "XXXX-3301"),
    ("V005", "Stark Logistics", "billing@starklogistics.test", "billing@starklogistics.test", "XXXX-7720"),
    ("V006", "Hooli", "invoices@hooli.test", "invoices@hooli.test", "XXXX-5054"),
]

INVOICES = [
    ("V003", "UMB-5531", 12400.00, "INR", "2026-09-20", "2026-10-20", "Unpaid"),
    ("V006", "HL-881", 7800.00, "INR", "2026-09-15", "2026-10-15", "Unpaid"),
    ("V001", "GX-2177", 39900.00, "INR", "2026-08-31", "2026-09-30", "Paid"),
    ("V002", "INI-0098", 15000.00, "INR", "2026-10-12", "2026-11-12", "Unpaid"),
]

GLOBEX_OLD = """<html><body><h1>GLOBEX CORPORATION</h1><h2>Tax Invoice</h2>
<table border=1>
<tr><th>Invoice No.</th><td>GX-2177</td></tr>
<tr><th>Invoice Date</th><td>31/08/2026</td></tr>
<tr><th>Due Date</th><td>30/09/2026</td></tr>
</table>
<table border=1><tr><th>Item</th><th>Amount</th></tr>
<tr><td>Cloud hosting - August</td><td>39,900.00</td></tr></table>
<p><b>Total payable: INR 39,900.00</b></p></body></html>"""

GLOBEX_NEW = """<html><body><h1>GLOBEX CORPORATION</h1><h2>Tax Invoice</h2>
<table border=1>
<tr><th>Invoice No.</th><td>GX-2291</td></tr>
<tr><th>Invoice Date</th><td>01/10/2026</td></tr>
<tr><th>Due Date</th><td>31 Oct 2026</td></tr>
<tr><th>Bill To</th><td>Acme Corp, Accounts Payable</td></tr>
</table>
<table border=1><tr><th>Item</th><th>Qty</th><th>Amount</th></tr>
<tr><td>Cloud hosting - September</td><td>1</td><td>42,000.00</td></tr>
<tr><td>Premium support</td><td>1</td><td>6,250.00</td></tr></table>
<p>Subtotal: 48,250.00<br><b>Total payable: INR 48,250.00</b></p>
<p>Please quote the invoice number with your payment.</p></body></html>"""

INITECH = """<html><body><div style="font-family:serif">
<p>INITECH LTD &middot; Professional Services</p>
<p>This invoice (reference <b>INI-0112</b>), issued on the 2nd of October 2026, covers the
TPS reporting automation engagement for Q3. The total amount of <b>&#8377;2,40,000.00</b>
(two lakh forty thousand rupees) is due on or before <b>2026-11-04</b>.</p>
<p>Bank details are unchanged from previous invoices.</p></div></body></html>"""

STARK_IND = """STARK INDUSTRIES - INVOICE
=========================
Invoice #: SI-7730
Date: Oct 4, 2026
Payment due: Oct 25, 2026

  Arc reactor maintenance kit ......... Rs. 18,500

AMOUNT DUE: Rs. 18,500
"""

STARK_LOG = """<html><body><h1>Stark Logistics</h1>
<dl><dt>Invoice</dt><dd>SL-0419</dd><dt>Issued</dt><dd>07-10-2026</dd>
<dt>Due</dt><dd>06-11-2026</dd></dl>
<p>Freight: Pune &rarr; Bhubaneswar, 3 pallets</p>
<p><strong>Amount due: INR 9,350.50</strong></p></body></html>"""

UMBRELLA_REMINDER = """<html><body><h1>Umbrella Corp</h1>
<p>Invoice UMB-5531 &middot; Amount INR 12,400.00 &middot; Due 2026-10-20</p></body></html>"""

# (id, folder, sender, subject, received, body, attachments[(filename, type, content)])
EMAILS = [
    (1, "Accounts Payable", "billing@globex.test", "Invoice GX-2177 for August", "2026-09-01 10:12",
     "Hello Acme team,\n\nPlease find attached our invoice for August.\n\nRegards,\nGlobex Billing",
     [("GX-2177.html", "text/html", GLOBEX_OLD)]),
    (2, "Accounts Payable", "newsletter@saasweekly.test", "This week in SaaS: 10 tools you need", "2026-10-02 07:00",
     "Top stories this week... (unsubscribe)", []),
    (3, "Accounts Payable", "billing@globex.test", "Invoice GX-2291 for September services", "2026-10-03 09:41",
     "Hello Acme team,\n\nAttached is invoice GX-2291 for September. Thank you for your business.\n\nGlobex Billing",
     [("GX-2291.html", "text/html", GLOBEX_NEW)]),
    (4, "Accounts Payable", "ar@stark-industries.test", "Invoice SI-7730", "2026-10-04 15:20",
     "Hi,\nInvoice attached as text.\n- Stark Industries AR", [("SI-7730.txt", "text/plain", STARK_IND)]),
    (5, "Accounts Payable", "accounts@initech.test", "Initech invoice INI-0112 - Q3 TPS automation", "2026-10-05 11:03",
     "Dear Acme,\n\nPlease see the attached invoice for the Q3 engagement.\n\nBest,\nInitech Accounts",
     [("INI-0112.html", "text/html", INITECH)]),
    (6, "Accounts Payable", "finance@umbrella-corp.test", "Reminder: Invoice UMB-5531", "2026-10-06 08:15",
     "Friendly reminder that invoice UMB-5531 is due on 20 October.", [("UMB-5531.html", "text/html", UMBRELLA_REMINDER)]),
    (7, "Accounts Payable", "finance@umbrella-corp.test", "Updated remittance details", "2026-10-06 16:47",
     "Hello Acme AP,\n\nPlease note that from this month, remittance advice for all Umbrella Corp payments should be sent "
     "to payments@umbrella-corp.test instead of finance@umbrella-corp.test. Our bank account is unchanged.\n\n"
     "Kind regards,\nUmbrella Corp Finance", []),
    (8, "Accounts Payable", "billing@starklogistics.test", "Stark Logistics invoice SL-0419", "2026-10-07 13:30",
     "Hi team, invoice for last week's freight is attached.", [("SL-0419.html", "text/html", STARK_LOG)]),
    (9, "Support", "priya.n@customer-one.test", "URGENT: cannot log in, dashboard down for whole team", "2026-10-08 09:02",
     "Since this morning none of our 40 users can log in to the Acme dashboard. We get a 502 error. This is blocking our month-end close.", []),
    (10, "Support", "rahul@customer-two.test", "Question about our billing date", "2026-10-08 11:45",
     "Hi, could you tell me whether our subscription renews on the 1st or the 15th? We want to align it with our budget cycle.", []),
    (11, "Support", "meera@customer-three.test", "Feature request: dark mode", "2026-10-09 18:20",
     "Love the product! Any plans for a dark mode? My eyes would thank you.", []),
]


def seed(db_path: str | Path, scenario=None) -> None:
    """Create a fresh database. ``scenario`` (a ``sim_env.scenarios.Scenario``) may rewrite part of the data afterwards."""
    db_path = Path(db_path)
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO vendors VALUES (?,?,?,?,?)", VENDORS)
    con.executemany(
        "INSERT INTO invoices (vendor_id, invoice_number, amount, currency, invoice_date, due_date, status) "
        "VALUES (?,?,?,?,?,?,?)",
        INVOICES,
    )
    att_id = 1
    for eid, folder, sender, subject, received, body, atts in EMAILS:
        con.execute("INSERT INTO emails VALUES (?,?,?,?,?,?,1)", (eid, folder, sender, subject, received, body))
        for filename, ctype, content in atts:
            con.execute("INSERT INTO attachments VALUES (?,?,?,?,?)", (att_id, eid, filename, ctype, content))
            att_id += 1
    if scenario is not None:
        scenario.apply(con)
    con.commit()
    con.close()

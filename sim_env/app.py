"""A small simulated company intranet: webmail, an ERP and a helpdesk.

It is a real web application (server-rendered HTML forms, cookie sessions,
server-side validation, a SQLite database) so the agent has to operate it the
way a person would, through a browser. Nothing about it is tailored to the
agent: there are no hidden hints or agent-only endpoints.

Fault injection makes the environment misbehave on purpose so reliability can
be measured:

* ``submit_503``: the first N invoice submissions fail with 503 and are NOT saved.
* ``submit_504_committed``: the first N invoice submissions ARE saved but the
  user sees a 504 gateway timeout (the classic "did it go through?" case).
* ``session_ttl``: the ERP session expires after N authenticated requests.

Independently of faults, the invoice form can be served in different UI variants (renamed labels,
reordered fields, other date formats, a two-step confirm flow); see ``sim_env/variants.py``.
"""

from __future__ import annotations

import html
import json
import re
import secrets
import sqlite3
import threading
from datetime import datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .variants import UiVariant

ERP_USER = "ap.agent"
ERP_PASSWORD = "acme-ap-2026"  # fictional sandbox credential
FIELDS_AFTER_VENDOR = ("invoice_number", "amount", "currency", "invoice_date", "due_date", "notes")

CSS = """
body{font-family:system-ui,sans-serif;margin:0;color:#1d2433}
header{background:#24344d;color:#fff;padding:10px 20px}
header a{color:#cfe0ff;margin-right:16px;text-decoration:none}
main{padding:20px;max-width:1000px}
table{border-collapse:collapse;margin:10px 0}td,th{border:1px solid #ccd;padding:6px 10px;text-align:left}
.error{background:#fde8e8;color:#8a1c1c;padding:8px;border:1px solid #f3b4b4;margin:8px 0}
.ok{background:#e6f6ea;color:#1c5b2a;padding:8px;border:1px solid #a9dcb5;margin:8px 0}
label{display:block;margin-top:10px;font-weight:600}
input,select,textarea{padding:6px;min-width:280px}
.unread{font-weight:700}
.notice{background:#fff7e0;color:#6b4e00;padding:8px;border:1px solid #ecd48a;margin:8px 0}
"""


def esc(v: object) -> str:
    return html.escape(str(v), quote=True)


class SimState:
    def __init__(self, db_path: Path, faults: dict | None = None, ui: "str | UiVariant | None" = None):
        self.db_path = Path(db_path)
        self.faults = dict(faults or {})
        self.ui = UiVariant.parse(ui)
        self.sessions: dict[str, int] = {}  # token -> authenticated requests served
        self.counters: dict[str, int] = {}
        self.lock = threading.Lock()

    def db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def take_fault(self, name: str) -> bool:
        """Consume one occurrence of a counted fault. True if it should fire."""
        with self.lock:
            budget = int(self.faults.get(name, 0))
            used = self.counters.get(name, 0)
            if used < budget:
                self.counters[name] = used + 1
                return True
            return False


def make_handler(state: SimState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AcmeIntranet/1.0"

        def log_message(self, *args):  # keep test output quiet
            pass

        # ---------- plumbing ----------
        def _send(self, status: int, body: str, ctype: str = "text/html; charset=utf-8", headers=None):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _page(self, title: str, body: str, status: int = 200, headers=None):
            nav = ('<header><b>Acme Corp Intranet</b> &nbsp; <a href="/mail">Mail</a>'
                   '<a href="/erp">ERP</a><a href="/helpdesk">Helpdesk</a></header>')
            doc = f"<!doctype html><html><head><title>{esc(title)}</title><style>{CSS}</style></head><body>{nav}<main>{body}</main></body></html>"
            self._send(status, doc, headers=headers)

        def _redirect(self, location: str, headers=None):
            h = {"Location": location}
            h.update(headers or {})
            self._send(303, "", headers=h)

        def _json(self, obj, status=200):
            self._send(status, json.dumps(obj, indent=2), "application/json")

        def _form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode()
            return {k: v[0].strip() for k, v in parse_qs(raw, keep_blank_values=True).items()}

        def _session_ok(self) -> tuple[bool, str | None]:
            jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            token = jar["erp_session"].value if "erp_session" in jar else None
            if not token or token not in state.sessions:
                return False, None
            with state.lock:
                state.sessions[token] += 1
                ttl = int(state.faults.get("session_ttl", 0))
                if ttl and state.sessions[token] > ttl and not state.counters.get("session_expired"):
                    del state.sessions[token]
                    state.counters["session_expired"] = 1
                    return False, "expired"
            return True, token

        def _require_login(self) -> bool:
            ok, why = self._session_ok()
            if not ok:
                nxt = self.path
                q = f"?next={nxt}" + ("&expired=1" if why == "expired" else "")
                self._redirect("/erp/login" + q)
            return ok

        # ---------- routing ----------
        def do_GET(self):
            url = urlparse(self.path)
            p, q = url.path.rstrip("/") or "/", parse_qs(url.query)
            try:
                if p == "/":
                    return self._page("Acme Intranet", "<h1>Acme Corp Intranet</h1><ul>"
                                      '<li><a href="/mail">Mail (ap@acme.test, support@acme.test)</a></li>'
                                      '<li><a href="/erp">ERP - Accounts Payable</a></li>'
                                      '<li><a href="/helpdesk">Helpdesk</a></li></ul>')
                if p == "/mail":
                    return self.mail_list(q.get("folder", [None])[0])
                m = re.fullmatch(r"/mail/(\d+)", p)
                if m:
                    return self.mail_view(int(m.group(1)))
                m = re.fullmatch(r"/mail/(\d+)/attachments/(\d+)", p)
                if m:
                    return self.attachment(int(m.group(1)), int(m.group(2)))
                if p == "/erp/login":
                    return self.login_form(q.get("next", ["/erp"])[0], "expired" in q)
                if p == "/erp/logout":
                    return self._redirect("/erp/login", {"Set-Cookie": "erp_session=; Path=/; Max-Age=0"})
                if p.startswith("/erp"):
                    if not self._require_login():
                        return
                    if p == "/erp":
                        return self._page("ERP", "<h1>ERP - Accounts Payable</h1><ul>"
                                          '<li><a href="/erp/invoices">Vendor invoices</a></li>'
                                          f'<li><a href="/erp/invoices/new">{esc(state.ui.new_invoice_link)}</a></li>'
                                          '<li><a href="/erp/vendors">Vendor master</a></li></ul>'
                                          '<p><a href="/erp/logout">Log out</a></p>')
                    if p == "/erp/invoices":
                        return self.invoice_list(q.get("q", [""])[0])
                    if p == "/erp/invoices/new":
                        return self.invoice_form({}, [])
                    m = re.fullmatch(r"/erp/invoices/(\d+)", p)
                    if m:
                        return self.invoice_view(int(m.group(1)), "created" in q)
                    if p == "/erp/vendors":
                        return self.vendor_list()
                    m = re.fullmatch(r"/erp/vendors/(V\d+)/edit", p)
                    if m:
                        return self.vendor_form(m.group(1), None, [])
                if p == "/helpdesk":
                    return self.ticket_list("created" in q)
                if p == "/helpdesk/new":
                    return self.ticket_form({}, [])
                if p.startswith("/api/"):
                    return self.api(p)
                self._page("Not found", "<h1>404 Not Found</h1>", 404)
            except Exception as e:  # pragma: no cover - surfaced to the client like a real server
                self._page("Server error", f"<h1>500 Internal Server Error</h1><pre>{esc(e)}</pre>", 500)

        def do_POST(self):
            p = urlparse(self.path).path.rstrip("/")
            form = self._form()
            if p == "/erp/login":
                return self.login_submit(form)
            if p.startswith("/erp"):
                if not self._require_login():
                    return
                if p == "/erp/invoices/new":
                    return self.invoice_submit(form)
                if p == "/erp/invoices/confirm":
                    return self.invoice_confirm(form)
                m = re.fullmatch(r"/erp/vendors/(V\d+)/edit", p)
                if m:
                    return self.vendor_submit(m.group(1), form)
            if p == "/helpdesk/new":
                return self.ticket_submit(form)
            self._page("Not found", "<h1>404 Not Found</h1>", 404)

        # ---------- mail ----------
        def mail_list(self, folder):
            con = state.db()
            folders = [r[0] for r in con.execute("SELECT DISTINCT folder FROM emails ORDER BY folder")]
            rows = con.execute(
                "SELECT * FROM emails WHERE (? IS NULL OR folder=?) ORDER BY received DESC", (folder, folder)
            ).fetchall()
            links = " | ".join(f'<a href="/mail?folder={esc(f)}">{esc(f)}</a>' for f in folders)
            trs = "".join(
                f'<tr class="{"unread" if r["unread"] else ""}"><td>{esc(r["received"])}</td><td>{esc(r["folder"])}</td>'
                f'<td>{esc(r["sender"])}</td><td><a href="/mail/{r["id"]}">{esc(r["subject"])}</a></td></tr>'
                for r in rows
            )
            self._page("Mail", f"<h1>Mail{': ' + esc(folder) if folder else ''}</h1><p>Folders: <a href='/mail'>All</a> | {links}</p>"
                       f"<table><tr><th>Received</th><th>Folder</th><th>From</th><th>Subject</th></tr>{trs}</table>")

        def mail_view(self, eid):
            con = state.db()
            r = con.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
            if not r:
                return self._page("Not found", "<h1>Email not found</h1>", 404)
            con.execute("UPDATE emails SET unread=0 WHERE id=?", (eid,))
            con.commit()
            atts = con.execute("SELECT id, filename FROM attachments WHERE email_id=?", (eid,)).fetchall()
            att_html = "".join(f'<li><a href="/mail/{eid}/attachments/{a["id"]}">{esc(a["filename"])}</a></li>' for a in atts)
            self._page(r["subject"], f"<h1>{esc(r['subject'])}</h1><p><b>From:</b> {esc(r['sender'])}<br><b>Received:</b> "
                       f"{esc(r['received'])}<br><b>Folder:</b> {esc(r['folder'])}</p><pre style='white-space:pre-wrap'>{esc(r['body'])}</pre>"
                       + (f"<h3>Attachments</h3><ul>{att_html}</ul>" if atts else "")
                       + '<p><a href="/mail">Back to inbox</a></p>')

        def attachment(self, eid, aid):
            r = state.db().execute("SELECT * FROM attachments WHERE id=? AND email_id=?", (aid, eid)).fetchone()
            if not r:
                return self._page("Not found", "<h1>Attachment not found</h1>", 404)
            ctype = "text/html; charset=utf-8" if r["content_type"] == "text/html" else "text/plain; charset=utf-8"
            self._send(200, r["content"], ctype)

        # ---------- ERP auth ----------
        def login_form(self, nxt, expired, error=None):
            msg = '<div class="error">Your session has expired. Please sign in again.</div>' if expired else ""
            if error:
                msg += f'<div class="error">{esc(error)}</div>'
            self._page("ERP sign in", f"<h1>Sign in to ERP</h1>{msg}<form method=post action='/erp/login'>"
                       f"<input type=hidden name=next value='{esc(nxt)}'>"
                       "<label for=username>Username</label><input id=username name=username>"
                       "<label for=password>Password</label><input id=password name=password type=password>"
                       "<p><button type=submit>Sign in</button></p></form>")

        def login_submit(self, form):
            if form.get("username") == ERP_USER and form.get("password") == ERP_PASSWORD:
                token = secrets.token_hex(16)
                state.sessions[token] = 0
                nxt = form.get("next") or "/erp"
                if not nxt.startswith("/erp") or nxt.startswith("/erp/login"):
                    nxt = "/erp"
                return self._redirect(nxt, {"Set-Cookie": f"erp_session={token}; Path=/; HttpOnly"})
            self.login_form(form.get("next", "/erp"), False, "Invalid username or password.")

        # ---------- ERP invoices ----------
        def invoice_list(self, query):
            con = state.db()
            rows = con.execute(
                "SELECT i.*, v.name vendor FROM invoices i JOIN vendors v ON v.id=i.vendor_id "
                "WHERE ?='' OR i.invoice_number LIKE ? OR v.name LIKE ? ORDER BY i.id DESC",
                (query, f"%{query}%", f"%{query}%"),
            ).fetchall()
            trs = "".join(
                f'<tr><td><a href="/erp/invoices/{r["id"]}">{r["id"]}</a></td><td>{esc(r["vendor"])}</td><td>{esc(r["invoice_number"])}</td>'
                f'<td>{r["amount"]:.2f} {esc(r["currency"])}</td><td>{esc(r["invoice_date"])}</td><td>{esc(r["due_date"])}</td><td>{esc(r["status"])}</td></tr>'
                for r in rows
            )
            self._page("Vendor invoices", "<h1>Vendor invoices</h1><form method=get><label for=q>Search by invoice number or vendor</label>"
                       f"<input id=q name=q value='{esc(query)}'> <button type=submit>Search</button></form>"
                       f'<p><a href="/erp/invoices/new">{esc(state.ui.new_invoice_link)}</a></p>'
                       "<table><tr><th>ERP ID</th><th>Vendor</th><th>Invoice number</th><th>Amount</th><th>Invoice date</th><th>Due date</th><th>Status</th></tr>"
                       f"{trs}</table>")

        def invoice_view(self, iid, created):
            r = state.db().execute(
                "SELECT i.*, v.name vendor FROM invoices i JOIN vendors v ON v.id=i.vendor_id WHERE i.id=?", (iid,)
            ).fetchone()
            if not r:
                return self._page("Not found", "<h1>Invoice not found</h1>", 404)
            ok = f'<div class="ok">Invoice saved successfully as ERP ID {iid}.</div>' if created else ""
            self._page(f"Invoice {r['invoice_number']}", f"<h1>Invoice {esc(r['invoice_number'])}</h1>{ok}<table>"
                       f"<tr><th>ERP ID</th><td>{iid}</td></tr><tr><th>Vendor</th><td>{esc(r['vendor'])}</td></tr>"
                       f"<tr><th>Amount</th><td>{r['amount']:.2f} {esc(r['currency'])}</td></tr>"
                       f"<tr><th>Invoice date</th><td>{esc(r['invoice_date'])}</td></tr><tr><th>Due date</th><td>{esc(r['due_date'])}</td></tr>"
                       f"<tr><th>Status</th><td>{esc(r['status'])}</td></tr><tr><th>Notes</th><td>{esc(r['notes'])}</td></tr></table>"
                       '<p><a href="/erp/invoices">All invoices</a></p>')

        def invoice_form(self, values, errors, status=200):
            ui = state.ui
            vendors = state.db().execute("SELECT id, name FROM vendors ORDER BY name").fetchall()
            opts = '<option value="">-- choose vendor --</option>' + "".join(
                f'<option value="{v["id"]}"{" selected" if values.get("vendor") == v["id"] else ""}>{esc(v["name"])}</option>' for v in vendors
            )
            cur = "".join(f'<option{" selected" if values.get("currency", "INR") == c else ""}>{c}</option>' for c in ("INR", "USD", "EUR"))
            err = "".join(f'<div class="error">{esc(e)}</div>' for e in errors)
            g = lambda k: esc(values.get(k, ""))  # noqa: E731
            date_ph = f" placeholder='{esc(ui.date_placeholder)}'"
            widgets = {
                "vendor": f"<select id=vendor name=vendor>{opts}</select>",
                "invoice_number": f"<input id=invoice_number name=invoice_number value='{g('invoice_number')}'>",
                "amount": f"<input id=amount name=amount value='{g('amount')}' placeholder='e.g. 1234.50'>",
                "currency": f"<select id=currency name=currency>{cur}</select>",
                "invoice_date": f"<input id=invoice_date name=invoice_date value='{g('invoice_date')}'{date_ph}>",
                "due_date": f"<input id=due_date name=due_date value='{g('due_date')}'{date_ph}>",
                "notes": f"<textarea id=notes name=notes>{g('notes')}</textarea>",
            }
            rows = "".join(f"<label for={k}>{esc(ui.labels[k])}</label>{widgets[k]}" for k in ui.order)
            banner = f'<div class="notice">{esc(ui.banner)}</div>' if ui.banner else ""
            self._page("New vendor invoice", f"<h1>{esc(ui.new_invoice_link)}</h1>{banner}{err}<form method=post action='/erp/invoices/new'>"
                       f"{rows}<p><button type=submit>{esc(ui.save_button)}</button></p></form>", status)

        def _validate_invoice(self, f):
            """Server-side validation shared by the first submit and the confirm step."""
            ui, errors, dates = state.ui, [], {}
            if not f.get("vendor"):
                errors.append("Vendor is required.")
            if not re.fullmatch(r"[A-Za-z0-9-]+", f.get("invoice_number", "")):
                errors.append("Vendor invoice number is required (letters, digits and dashes only).")
            if not re.fullmatch(r"\d+(\.\d{1,2})?", f.get("amount", "")):
                errors.append("Amount must be a plain number like 1234.50 (no commas or currency symbols).")
            for k in ("invoice_date", "due_date"):
                try:
                    dates[k] = datetime.strptime(f.get(k, ""), ui.date_strptime).date()
                except ValueError:
                    errors.append(f"{ui.labels[k]} must be in {ui.date_placeholder} format.")
            if len(dates) == 2 and dates["due_date"] < dates["invoice_date"]:
                errors.append("Due date cannot be before the invoice date.")
            if not errors:
                dup = state.db().execute("SELECT id FROM invoices WHERE vendor_id=? AND upper(invoice_number)=upper(?)",
                                         (f["vendor"], f["invoice_number"])).fetchone()
                if dup:
                    errors.append(f"An invoice with this number already exists for this vendor (ERP ID {dup['id']}).")
            return errors, dates

        def invoice_submit(self, f):
            errors, dates = self._validate_invoice(f)
            if errors:
                return self.invoice_form(f, errors, 422)
            if state.ui.two_step:
                return self.invoice_review(f)
            self._commit_invoice(f, dates)

        def invoice_review(self, f):
            """Step two of the two-step flow: nothing has been stored yet."""
            ui = state.ui
            vendor = state.db().execute("SELECT name FROM vendors WHERE id=?", (f["vendor"],)).fetchone()["name"]
            shown = {"vendor": vendor, **{k: f.get(k, "") for k in FIELDS_AFTER_VENDOR}}
            rows = "".join(f"<tr><th>{esc(ui.labels[k])}</th><td>{esc(shown[k])}</td></tr>" for k in ui.order)
            hidden = "".join(f"<input type=hidden name='{k}' value='{esc(f.get(k, ''))}'>" for k in ("vendor", *FIELDS_AFTER_VENDOR))
            self._page("Review invoice", "<h1>Review invoice</h1><p>Nothing has been posted yet. Check the details and confirm.</p>"
                       f"<table>{rows}</table><form method=post action='/erp/invoices/confirm'>{hidden}"
                       "<p><button type=submit>Confirm and post</button></p></form>"
                       '<p><a href="/erp/invoices/new">Cancel and start over</a></p>')

        def invoice_confirm(self, f):
            errors, dates = self._validate_invoice(f)
            if errors:
                return self.invoice_form(f, errors, 422)
            self._commit_invoice(f, dates)

        def _commit_invoice(self, f, dates):
            con = state.db()
            if state.take_fault("submit_503"):
                return self._page("Service unavailable", "<h1>503 Service Unavailable</h1><p>The ERP is temporarily overloaded. Please try again.</p>", 503)
            cur = con.execute(
                "INSERT INTO invoices (vendor_id, invoice_number, amount, currency, invoice_date, due_date, status, notes, created_by) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f["vendor"], f["invoice_number"], float(f["amount"]), f.get("currency", "INR"),
                 dates["invoice_date"].isoformat(), dates["due_date"].isoformat(), "Unpaid", f.get("notes", ""), ERP_USER),
            )
            con.execute("INSERT INTO audit_log (actor, action, detail) VALUES (?,?,?)",
                        (ERP_USER, "invoice.create", json.dumps({"id": cur.lastrowid, "number": f["invoice_number"]})))
            con.commit()
            if state.take_fault("submit_504_committed"):
                return self._page("Gateway timeout", "<h1>504 Gateway Timeout</h1><p>The upstream server did not respond in time.</p>", 504)
            self._redirect(f"/erp/invoices/{cur.lastrowid}?created=1")

        # ---------- ERP vendors ----------
        def vendor_list(self):
            rows = state.db().execute("SELECT * FROM vendors ORDER BY name").fetchall()
            trs = "".join(
                f'<tr><td>{r["id"]}</td><td>{esc(r["name"])}</td><td>{esc(r["contact_email"])}</td><td>{esc(r["remittance_email"])}</td>'
                f'<td>{esc(r["bank_account"])}</td><td><a href="/erp/vendors/{r["id"]}/edit">Edit {esc(r["name"])}</a></td></tr>'
                for r in rows
            )
            self._page("Vendor master", "<h1>Vendor master</h1><table><tr><th>ID</th><th>Name</th><th>Contact email</th>"
                       f"<th>Remittance email</th><th>Bank account</th><th></th></tr>{trs}</table>")

        def vendor_form(self, vid, values, errors, saved=False, status=200):
            r = state.db().execute("SELECT * FROM vendors WHERE id=?", (vid,)).fetchone()
            if not r:
                return self._page("Not found", "<h1>Vendor not found</h1>", 404)
            v = dict(r) | (values or {})
            err = "".join(f'<div class="error">{esc(e)}</div>' for e in errors)
            ok = '<div class="ok">Vendor details saved.</div>' if saved else ""
            self._page(f"Edit vendor {r['name']}", f"<h1>Edit vendor: {esc(r['name'])}</h1>{ok}{err}<form method=post>"
                       f"<label for=contact_email>Contact email</label><input id=contact_email name=contact_email value='{esc(v['contact_email'])}'>"
                       f"<label for=remittance_email>Remittance email</label><input id=remittance_email name=remittance_email value='{esc(v['remittance_email'])}'>"
                       f"<label for=bank_account>Bank account</label><input id=bank_account name=bank_account value='{esc(v['bank_account'])}'>"
                       "<p><button type=submit>Save vendor</button></p></form><p><a href='/erp/vendors'>Vendor master</a></p>", status)

        def vendor_submit(self, vid, f):
            errors = [f"{k.replace('_', ' ').capitalize()} must be a valid email address."
                      for k in ("contact_email", "remittance_email") if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", f.get(k, ""))]
            if errors:
                return self.vendor_form(vid, f, errors, status=422)
            con = state.db()
            con.execute("UPDATE vendors SET contact_email=?, remittance_email=?, bank_account=? WHERE id=?",
                        (f["contact_email"], f["remittance_email"], f.get("bank_account", ""), vid))
            con.execute("INSERT INTO audit_log (actor, action, detail) VALUES (?,?,?)", (ERP_USER, "vendor.update", json.dumps({"id": vid, **f})))
            con.commit()
            self.vendor_form(vid, None, [], saved=True)

        # ---------- helpdesk ----------
        def ticket_list(self, created):
            rows = state.db().execute("SELECT * FROM tickets ORDER BY id").fetchall()
            trs = "".join(f"<tr><td>{r['id']}</td><td>{esc(r['customer_email'])}</td><td>{esc(r['subject'])}</td>"
                          f"<td>{esc(r['priority'])}</td><td>{esc(r['category'])}</td></tr>" for r in rows)
            ok = '<div class="ok">Ticket created.</div>' if created else ""
            self._page("Helpdesk", f"<h1>Helpdesk tickets</h1>{ok}<p><a href='/helpdesk/new'>Create a ticket</a></p>"
                       f"<table><tr><th>#</th><th>Customer</th><th>Subject</th><th>Priority</th><th>Category</th></tr>{trs}</table>")

        def ticket_form(self, values, errors, status=200):
            g = lambda k: esc(values.get(k, ""))  # noqa: E731
            sel = lambda name, opts: "".join(  # noqa: E731
                f"<option{' selected' if values.get(name) == o else ''}>{o}</option>" for o in opts)
            err = "".join(f'<div class="error">{esc(e)}</div>' for e in errors)
            self._page("New ticket", f"<h1>Create a ticket</h1>{err}<form method=post action='/helpdesk/new'>"
                       f"<label for=customer_email>Customer email</label><input id=customer_email name=customer_email value='{g('customer_email')}'>"
                       f"<label for=subject>Subject</label><input id=subject name=subject value='{g('subject')}'>"
                       f"<label for=priority>Priority</label><select id=priority name=priority><option value=''>-- choose --</option>{sel('priority', ['Low', 'Medium', 'High'])}</select>"
                       f"<label for=category>Category</label><select id=category name=category><option value=''>-- choose --</option>{sel('category', ['Outage', 'Billing', 'Feature request', 'Other'])}</select>"
                       f"<label for=description>Description</label><textarea id=description name=description>{g('description')}</textarea>"
                       "<p><button type=submit>Create ticket</button></p></form>", status)

        def ticket_submit(self, f):
            errors = [f"{k.replace('_', ' ').capitalize()} is required." for k in
                      ("customer_email", "subject", "priority", "category", "description") if not f.get(k)]
            if errors:
                return self.ticket_form(f, errors, 422)
            con = state.db()
            src = con.execute("SELECT id FROM emails WHERE sender=? AND folder='Support'", (f["customer_email"],)).fetchone()
            con.execute("INSERT INTO tickets (customer_email, subject, priority, category, description, source_email_id) VALUES (?,?,?,?,?,?)",
                        (f["customer_email"], f["subject"], f["priority"], f["category"], f["description"], src["id"] if src else None))
            con.commit()
            self._redirect("/helpdesk?created=1")

        # ---------- read-only JSON API ----------
        def api(self, p):
            con = state.db()
            if p == "/api/invoices":
                rows = con.execute("SELECT i.*, v.name vendor FROM invoices i JOIN vendors v ON v.id=i.vendor_id ORDER BY i.id").fetchall()
                return self._json([dict(r) for r in rows])
            if p == "/api/vendors":
                return self._json([dict(r) for r in con.execute("SELECT * FROM vendors")])
            if p == "/api/tickets":
                return self._json([dict(r) for r in con.execute("SELECT * FROM tickets")])
            self._json({"error": "not found"}, 404)

    return Handler


class SimServer:
    """Run the intranet in a background thread (used by the CLI, evals and tests)."""

    def __init__(self, db_path: str | Path, port: int = 0, faults: dict | None = None, ui: "str | UiVariant | None" = None):
        self.state = SimState(Path(db_path), faults, ui)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(self.state))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    start = __enter__

    def stop(self):
        self.__exit__()

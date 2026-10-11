"""Browser tools built on Playwright (headless Chromium).

Observation design: instead of screenshots, every action returns a compact
text snapshot of the page: URL, HTTP status, visible text, and a numbered list
of interactive elements with their labels, current values and options. The
model refers to elements by that number (or by their visible label), which is
cheap, precise, and works on any site without site-specific code.

Every action that would submit a form is described as an ``Action`` and passed
through the approval gate first; secrets are typed from ``{{secret:name}}``
placeholders so their values never enter the model's context or the trace.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from ..agent.policy import Action
from .base import Tool, ToolResult, obj

SNAPSHOT_JS = r"""
() => {
  const sel = 'a[href],button,input,select,textarea,[role=button]';
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    const st = getComputedStyle(el);
    if (el.type === 'hidden' || st.display === 'none' || st.visibility === 'hidden') continue;
    i += 1;
    el.setAttribute('data-tp-ref', String(i));
    let label = '';
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) label = l.innerText.trim();
    }
    if (!label) label = el.getAttribute('aria-label') || '';
    const tag = el.tagName.toLowerCase();
    const item = {
      ref: i, tag, type: (el.getAttribute('type') || '').toLowerCase(), label,
      text: (el.innerText || el.value || '').trim().slice(0, 100),
      name: el.name || '', placeholder: el.placeholder || '',
      href: el.getAttribute('href') || '',
      submits: (tag === 'button' && (el.type || 'submit') === 'submit') || (tag === 'input' && el.type === 'submit'),
      form_method: el.form ? (el.form.getAttribute('method') || 'get').toLowerCase() : '',
    };
    if (tag === 'select') {
      item.options = [...el.options].map(o => o.text);
      item.value = el.selectedIndex >= 0 ? el.options[el.selectedIndex].text : '';
    } else if (tag === 'input' || tag === 'textarea') {
      item.value = el.type === 'password' ? (el.value ? '********' : '') : el.value;
    }
    out.push(item);
  }
  return {title: document.title, url: location.href, text: document.body ? document.body.innerText : '', elements: out};
}
"""

FORM_FIELDS_JS = r"""
(el) => {
  const form = el.form || el.closest('form');
  if (!form) return null;
  const fields = {};
  for (const f of form.elements) {
    if (!f.name || f.type === 'hidden' || f.type === 'submit' || f.tagName === 'BUTTON') continue;
    if (f.type === 'password') { fields[f.name] = f.value ? '********' : ''; continue; }
    fields[f.name] = f.tagName === 'SELECT' ? (f.selectedIndex >= 0 ? f.options[f.selectedIndex].text : '') : f.value;
  }
  return {action: form.getAttribute('action') || location.pathname, method: (form.getAttribute('method') || 'get').toLowerCase(), fields};
}
"""

MAX_TEXT = 5000


class BrowserSession:
    def __init__(self, headless: bool = True):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        # Optional override for environments whose pre-installed Chromium does not match the Playwright version.
        exe = os.environ.get("TASKPILOT_CHROMIUM")
        try:
            self._browser = self._pw.chromium.launch(headless=headless, executable_path=exe or None)
        except Exception:
            self._pw.stop()  # otherwise the next attempt fails with a confusing event-loop error
            raise
        self.context = self._browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = self.context.new_page()
        self.page.set_default_timeout(10000)
        self.last_status: int | None = None
        self.last_elements: list[dict] = []
        self._owner = True
        self.page.on("response", self._on_response)

    def fork(self) -> "BrowserSession":
        """A second tab in the same browser context (shares cookies), used by the verifier
        so its browsing does not disturb the page the executor is working on."""
        other = object.__new__(BrowserSession)
        other._pw, other._browser, other.context = self._pw, self._browser, self.context
        other.page = self.context.new_page()
        other.page.set_default_timeout(10000)
        other.last_status, other.last_elements, other._owner = None, [], False
        other.page.on("response", other._on_response)
        return other

    def _on_response(self, response):
        try:
            if response.request.is_navigation_request() and response.frame == self.page.main_frame:
                self.last_status = response.status
        except Exception:
            pass

    def settle(self):
        try:
            self.page.wait_for_load_state("load", timeout=10000)
        except Exception:
            pass

    def snapshot(self) -> str:
        self.settle()
        data = self.page.evaluate(SNAPSHOT_JS)
        self.last_elements = data["elements"]
        text = re.sub(r"\n{3,}", "\n\n", data["text"]).strip()
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + "\n... [page text truncated]"
        lines = [f"URL: {data['url']}  (HTTP {self.last_status})", f"Title: {data['title']}", "--- Page text ---", text,
                 "--- Interactive elements (use the [number] as target) ---"]
        for e in data["elements"]:
            lines.append(describe_element(e))
        return "\n".join(lines)

    def close(self):
        if not self._owner:
            self.page.close()
            return
        try:
            self.context.close()
            self._browser.close()
        finally:
            self._pw.stop()


def describe_element(e: dict) -> str:
    e = {"label": "", "text": "", "placeholder": "", "name": "", "type": "", "href": "", "submits": False, **e}
    tag = e["tag"]
    name = e["label"] or e["text"] or e["placeholder"] or e["name"]
    if tag == "a":
        return f"[{e['ref']}] link \"{name}\" -> {e['href']}"
    if tag == "button" or e["type"] == "submit":
        return f"[{e['ref']}] button \"{name}\"" + (" (submits form)" if e["submits"] else "")
    if tag == "select":
        return f"[{e['ref']}] select \"{name}\" value=\"{e.get('value', '')}\" options: {' | '.join(e.get('options', []))}"
    kind = f"input({e['type'] or 'text'})" if tag == "input" else tag
    extra = f" placeholder=\"{e['placeholder']}\"" if e["placeholder"] else ""
    return f"[{e['ref']}] {kind} \"{name}\" value=\"{e.get('value', '')}\"{extra}"


def resolve_target(elements: list[dict], target) -> tuple[dict | None, str]:
    """Find an element by snapshot number or by visible label/text/name."""
    t = str(target).strip().strip("[]")
    if t.isdigit():
        for e in elements:
            if e["ref"] == int(t):
                return e, ""
        return None, f"No element [{t}] on the current page. Take a fresh snapshot."
    low = t.lower()
    keys = ("label", "text", "name", "placeholder")
    exact = [e for e in elements if any((e.get(k) or "").lower() == low for k in keys)]
    if len(exact) == 1:
        return exact[0], ""
    partial = exact or [e for e in elements if any(low in (e.get(k) or "").lower() for k in keys)]
    if len(partial) == 1:
        return partial[0], ""
    if not partial:
        return None, f"No element matching '{t}'. Use an element number from the snapshot."
    options = "; ".join(describe_element(e) for e in partial[:8])
    return None, f"'{t}' is ambiguous, candidates: {options}. Use the element number."


def _session(ctx) -> BrowserSession:
    if ctx.browser is None:
        ctx.browser = BrowserSession()
    return ctx.browser


def _origin_ok(ctx, url: str) -> bool:
    o = urlparse(url)
    return any(url.startswith(a) for a in ctx.allowed_origins) or o.scheme in {"about", "data"}


def _status_result(b: BrowserSession, note: str = "", *, was_submit: bool = False) -> ToolResult:
    snap = b.snapshot()
    status = b.last_status or 200
    body = (note + "\n" if note else "") + snap
    if status >= 500:
        if was_submit:
            body = (f"The server returned HTTP {status} after the form was submitted. The change MAY OR MAY NOT have been "
                    f"saved. Check the current state of the record before submitting again, to avoid duplicates.\n" + body)
        return ToolResult(False, body, "transient", safe_to_retry=not was_submit)
    if status == 422 or (status >= 400 and was_submit):
        return ToolResult(False, "The form was rejected; read the error messages on the page and fix the input.\n" + body, "invalid")
    if status >= 400:
        return ToolResult(False, body, "invalid")
    return ToolResult(True, body, safe_to_retry=not was_submit)


def _gate_submit(ctx, b: BrowserSession, element: dict) -> tuple[bool, str]:
    """If clicking ``element`` would POST a form, ask the approval gate."""
    if not (element.get("submits") and element.get("form_method") == "post"):
        return True, ""
    handle = b.page.locator(f"[data-tp-ref='{element['ref']}']")
    info = handle.evaluate(FORM_FIELDS_JS)
    if not info:
        return True, ""
    target = urlparse(urljoin(b.page.url, info["action"])).path
    action = Action("form_submit", target, element.get("text") or element.get("label", ""), info["fields"])
    ctx.trace.log("action", action=action.summary())
    return ctx.check_action(action)


class BrowserOpen(Tool):
    name = "browser_open"
    description = ("Open a URL in the browser and return a snapshot of the page (text plus numbered interactive elements). "
                   "Only company systems listed in the context are reachable.")
    input_schema = obj({"url": {"type": "string", "description": "Absolute URL, or a path relative to the current page"}}, ["url"])

    def run(self, ctx, url: str) -> ToolResult:
        b = _session(ctx)
        full = urljoin(b.page.url if b.page.url.startswith("http") else ctx.allowed_origins[0], url)
        if not _origin_ok(ctx, full):
            return ToolResult(False, f"{full} is outside the allowed systems: {ctx.allowed_origins}", "blocked")
        try:
            b.last_status = None
            b.page.goto(full, wait_until="load")
        except Exception as e:
            return ToolResult(False, f"Navigation failed: {e}", "transient", safe_to_retry=True)
        return _status_result(b)


class BrowserSnapshot(Tool):
    name = "browser_snapshot"
    description = "Re-read the current page without doing anything."
    input_schema = obj({})

    def run(self, ctx) -> ToolResult:
        b = _session(ctx)
        return ToolResult(True, b.snapshot(), safe_to_retry=True)


class BrowserClick(Tool):
    name = "browser_click"
    description = ("Click a link or button. target is the element number from the latest snapshot (preferred) or its "
                   "visible label. Returns the resulting page. Buttons that submit forms may require approval under company policy.")
    input_schema = obj({"target": {"type": "string"}}, ["target"])
    read_only = False

    def run(self, ctx, target: str) -> ToolResult:
        b = _session(ctx)
        if not b.last_elements:
            b.snapshot()
        el, err = resolve_target(b.last_elements, target)
        if not el:
            return ToolResult(False, err, "invalid")
        if el["tag"] == "a" and el["href"] and not _origin_ok(ctx, urljoin(b.page.url, el["href"])):
            return ToolResult(False, f"Link goes outside the allowed systems: {el['href']}", "blocked")
        allowed, msg = _gate_submit(ctx, b, el)
        if not allowed:
            return ToolResult(False, msg, "blocked")
        try:
            b.last_status = None
            b.page.locator(f"[data-tp-ref='{el['ref']}']").click()
        except Exception as e:
            return ToolResult(False, f"Click failed: {e}", "transient", safe_to_retry=not el.get("submits"))
        return _status_result(b, was_submit=bool(el.get("submits") and el.get("form_method") == "post"))


class BrowserFill(Tool):
    name = "browser_fill"
    description = (
        "Fill one or more form fields (text inputs, textareas and selects) and optionally click a submit button. "
        "For a select, value is the visible option text. Use {{secret:NAME}} as the value for credentials; the real "
        "secret is typed for you and never shown. Returns the resulting page."
    )
    input_schema = obj(
        {
            "fields": {
                "type": "array",
                "items": obj({"target": {"type": "string"}, "value": {"type": "string"}}, ["target", "value"]),
            },
            "submit": {"type": "string", "description": "Optional: element number or label of the button to click afterwards"},
        },
        ["fields"],
    )
    read_only = False

    def run(self, ctx, fields: list[dict], submit: str | None = None) -> ToolResult:
        b = _session(ctx)
        if not b.last_elements:
            b.snapshot()
        problems = []
        for f in fields:
            el, err = resolve_target(b.last_elements, f["target"])
            if not el:
                problems.append(err)
                continue
            loc = b.page.locator(f"[data-tp-ref='{el['ref']}']")
            value = ctx.resolve_secrets(f["value"])
            if "{{secret:" in value:
                problems.append(f"Unknown secret in {f['value']!r}. Available secrets: {sorted(ctx.secrets)}")
                continue
            try:
                if el["tag"] == "select":
                    try:
                        loc.select_option(label=value)
                    except Exception as err:
                        match = [o for o in el.get("options", []) if value.lower() in o.lower()]
                        if len(match) != 1:
                            raise ValueError(f"option {value!r} not found; options are {el.get('options')}") from err
                        loc.select_option(label=match[0])
                else:
                    loc.fill(value)
            except Exception as e:
                problems.append(f"Could not fill {f['target']!r}: {e}")
        if problems:
            return ToolResult(False, "\n".join(problems) + "\n" + b.snapshot(), "invalid")
        if submit:
            el, err = resolve_target(b.last_elements, submit)
            if not el:
                return ToolResult(False, err + "\nFields were filled but nothing was submitted.\n" + b.snapshot(), "invalid")
            allowed, msg = _gate_submit(ctx, b, el)
            if not allowed:
                return ToolResult(False, msg, "blocked")
            b.last_status = None
            try:
                b.page.locator(f"[data-tp-ref='{el['ref']}']").click()
            except Exception as e:
                return ToolResult(False, f"Submit click failed: {e}", "transient")
            return _status_result(b, "Fields filled and form submitted.", was_submit=bool(el.get("form_method") == "post"))
        return ToolResult(True, "Fields filled (not submitted).\n" + b.snapshot(), safe_to_retry=True)


class BrowserBack(Tool):
    name = "browser_back"
    description = "Go back to the previous page."
    input_schema = obj({})

    def run(self, ctx) -> ToolResult:
        b = _session(ctx)
        b.page.go_back()
        return ToolResult(True, b.snapshot(), safe_to_retry=True)


class BrowserScreenshot(Tool):
    name = "browser_screenshot"
    description = "Save a screenshot of the current page as evidence for the final report. Returns the file path."
    input_schema = obj({"name": {"type": "string", "description": "short file name, e.g. invoice-saved"}}, ["name"])

    def run(self, ctx, name: str) -> ToolResult:
        b = _session(ctx)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", name)[:60] or "screenshot"
        path = Path(ctx.run_dir) / "evidence" / f"{safe}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        b.page.screenshot(path=str(path), full_page=True)
        ctx.evidence.append(str(path))
        return ToolResult(True, f"Saved screenshot of {b.page.url} to {path}", safe_to_retry=True)


BROWSER_TOOLS = [BrowserOpen(), BrowserSnapshot(), BrowserClick(), BrowserFill(), BrowserBack(), BrowserScreenshot()]

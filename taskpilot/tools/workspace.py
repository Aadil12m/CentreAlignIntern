"""File, code, HTTP and bookkeeping tools."""

from __future__ import annotations

import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from ..agent.context import PlanStep
from ..agent.policy import Action
from .base import Tool, ToolResult, obj


def _resolve(ctx, path: str, *, write: bool) -> tuple[Path | None, str]:
    """Map an agent path to disk. 'company/...' is read-only knowledge; everything else is the workspace."""
    p = Path(path)
    if p.parts and p.parts[0] == "company":
        if write:
            return None, "company/ is read-only reference material."
        base, rel = ctx.company_dir, Path(*p.parts[1:]) if len(p.parts) > 1 else Path(".")
    else:
        base, rel = ctx.workspace, p
    full = (base / rel).resolve()
    if not str(full).startswith(str(base.resolve())):
        return None, "Path escapes the sandbox."
    return full, ""


class ListFiles(Tool):
    name = "list_files"
    description = "List files in the workspace (default) or in 'company' (read-only policies, procedures and system notes)."
    input_schema = obj({"path": {"type": "string", "default": "."}})

    def run(self, ctx, path: str = ".") -> ToolResult:
        full, err = _resolve(ctx, path, write=False)
        if not full:
            return ToolResult(False, err, "invalid")
        if not full.exists():
            return ToolResult(False, f"{path} does not exist", "invalid")
        items = sorted(str(p.relative_to(full)) + ("/" if p.is_dir() else "") for p in full.rglob("*"))
        return ToolResult(True, "\n".join(items) or "(empty)", safe_to_retry=True)


class ReadFile(Tool):
    name = "read_file"
    description = "Read a text file from the workspace or from company/."
    input_schema = obj({"path": {"type": "string"}}, ["path"])

    def run(self, ctx, path: str) -> ToolResult:
        full, err = _resolve(ctx, path, write=False)
        if not full:
            return ToolResult(False, err, "invalid")
        if not full.is_file():
            return ToolResult(False, f"{path} is not a file", "invalid")
        return ToolResult(True, full.read_text(errors="replace"), safe_to_retry=True)


class WriteFile(Tool):
    name = "write_file"
    description = "Create or overwrite a text file in the workspace (e.g. output/report.csv)."
    input_schema = obj({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])
    read_only = False

    def run(self, ctx, path: str, content: str) -> ToolResult:
        full, err = _resolve(ctx, path, write=True)
        if not full:
            return ToolResult(False, err, "blocked")
        allowed, msg = ctx.check_action(Action("file_write", str(Path(path)), fields={"bytes": str(len(content))}))
        if not allowed:
            return ToolResult(False, msg, "blocked")
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        return ToolResult(True, f"Wrote {len(content)} characters to {path}", safe_to_retry=True)


class RunPython(Tool):
    name = "run_python"
    description = ("Run a short Python 3 script in an isolated subprocess (cwd = workspace, 20 s timeout, no network "
                   "guarantees). Use it for parsing, date/number conversion, and checking files. Print what you need.")
    input_schema = obj({"code": {"type": "string"}}, ["code"])
    read_only = False

    def run(self, ctx, code: str) -> ToolResult:
        try:
            proc = subprocess.run([sys.executable, "-I", "-c", code], cwd=ctx.workspace, capture_output=True,
                                  text=True, timeout=20)
        except subprocess.TimeoutExpired:
            return ToolResult(False, "Script timed out after 20 s", "invalid")
        out = (proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr else "")).strip() or "(no output)"
        if proc.returncode != 0:
            return ToolResult(False, out, "invalid")
        return ToolResult(True, out, safe_to_retry=True)


class HttpGet(Tool):
    name = "http_get"
    description = "GET a URL from an allowed company system and return the body (useful for JSON APIs)."
    input_schema = obj({"url": {"type": "string"}}, ["url"])

    def run(self, ctx, url: str) -> ToolResult:
        if not any(url.startswith(o) for o in ctx.allowed_origins):
            return ToolResult(False, f"{url} is outside the allowed systems", "blocked")
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return ToolResult(True, r.read().decode(errors="replace"), safe_to_retry=True)
        except urllib.error.HTTPError as e:
            kind = "transient" if e.code >= 500 else "invalid"
            return ToolResult(False, f"HTTP {e.code}: {e.read().decode(errors='replace')[:500]}", kind, safe_to_retry=True)
        except (urllib.error.URLError, TimeoutError) as e:
            return ToolResult(False, f"Request failed: {e}", "transient", safe_to_retry=True)


class AskHuman(Tool):
    name = "ask_human"
    description = ("Ask your human supervisor a question. Use ONLY when you cannot proceed safely on your own: the request is "
                   "genuinely ambiguous (e.g. several records match), information is missing from every available source, "
                   "or you need a decision outside your authority. Give options when you can.")
    input_schema = obj({"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}, ["question"])

    def run(self, ctx, question: str, options: list[str] | None = None) -> ToolResult:
        ans = ctx.human.ask(question, options)
        ctx.human_log.append({"type": "question", "question": question, "options": options, "answer": ans.text})
        ctx.trace.log("human", question=question, answer=ans.text)
        return ToolResult(True, f"Human answered: {ans.text}")


class Remember(Tool):
    name = "remember"
    description = ("Save a fact you discovered to working memory (e.g. invoice_amount = 48250.00, erp_invoice_id = 7). "
                   "Working memory is shown to the verifier and in the final report.")
    input_schema = obj({"key": {"type": "string"}, "value": {"type": "string"}}, ["key", "value"])

    def run(self, ctx, key: str, value: str) -> ToolResult:
        ctx.memory.remember(key, value)
        return ToolResult(True, "Working memory now:\n" + ctx.memory.render(), safe_to_retry=True)


class UpdatePlan(Tool):
    name = "update_plan"
    description = "Replace your current plan. Call it when you start, when a step finishes, and whenever you change approach."
    input_schema = obj(
        {"steps": {"type": "array", "items": obj({
            "description": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "done", "blocked", "skipped"]},
        }, ["description", "status"])},
         "reason": {"type": "string"}},
        ["steps"],
    )

    def run(self, ctx, steps: list[dict], reason: str = "") -> ToolResult:
        ctx.plan = [PlanStep(s["description"], s["status"]) for s in steps]
        ctx.trace.log("plan", steps=[f"[{s.status}] {s.description}" for s in ctx.plan], reason=reason)
        return ToolResult(True, "Plan updated.", safe_to_retry=True)


class Finish(Tool):
    name = "finish"
    description = (
        "End the task. status=completed only after you have checked the system of record and confirmed the outcome. "
        "status=blocked when policy, a denied approval or missing information stops you; status=failed when you "
        "tried and could not do it. evidence: concrete proof such as record IDs, URLs, file paths and screenshot paths."
    )
    input_schema = obj(
        {
            "status": {"type": "string", "enum": ["completed", "blocked", "failed"]},
            "summary": {"type": "string", "description": "2-5 sentences for the requester: what was done and the result"},
            "evidence": {"type": "array", "items": {"type": "string"}},
        },
        ["status", "summary", "evidence"],
    )

    def run(self, ctx, status: str, summary: str, evidence: list[str]) -> ToolResult:
        ctx.finish = {"status": status, "summary": summary, "evidence": evidence}
        return ToolResult(True, "Finish recorded; the result will now be independently verified.", terminal=True)


WORKSPACE_TOOLS = [ListFiles(), ReadFile(), WriteFile(), RunPython(), HttpGet(), AskHuman(), Remember(), UpdatePlan(), Finish()]

"""The agent runtime: Goal -> Understand -> Plan -> Execute/Observe/Adapt -> Verify -> Complete.

The loop itself is plain code that the model cannot skip:

1. Understand: one structured call turns the request into a task spec with
   checkable success criteria and a first plan (asks the human first if the
   request cannot be started at all).
2. Execute: a tool-use loop. The harness enforces the approval gate, retries
   transient failures of idempotent actions, flags repeated failures, and
   stops runaway runs with a step budget.
3. Verify: a separate verifier conversation with read-only tools checks every
   success criterion against the real systems. If it fails, its findings go
   back to the executor for another round.
4. Complete: a report with summary, evidence, verification, human
   interactions, token usage and a full trace; lessons go to long-term memory.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..llm import LLMError, LLMProvider, Usage, tool_results_message
from ..tools import VERIFIER_TOOLS, ToolRegistry, ToolResult, default_registry
from . import prompts
from .context import RunContext
from .human import HumanChannel
from .memory import LongTermMemory
from .policy import PolicyGate
from .trace import Trace


@dataclass
class AgentConfig:
    max_steps: int = 40  # model turns in the execution phase, across verification rounds
    max_verify_rounds: int = 2  # how many times a failed verification is sent back for fixing
    max_transient_retries: int = 2
    repeat_failure_limit: int = 3  # identical failing call this many times -> forced change of approach
    max_consecutive_errors: int = 8
    understand: bool = True
    verify: bool = True
    reflect: bool = True
    echo: bool = True


@dataclass
class RunResult:
    run_id: str
    goal: str
    status: str  # completed | blocked | failed | unverified
    claimed_status: str | None
    summary: str
    evidence: list[str]
    verification: dict | None
    task_spec: dict | None
    plan: list[str]
    memory: dict
    human_interactions: list[dict]
    steps: int
    tool_calls: int
    tool_errors: int
    retries: int
    usage: dict
    seconds: float
    run_dir: str
    lessons: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


class Agent:
    def __init__(
        self,
        llm: LLMProvider,
        *,
        base_url: str,
        company_dir: str | Path,
        human: HumanChannel,
        secrets: dict[str, str] | None = None,
        runs_dir: str | Path = "runs",
        lessons_path: str | Path | None = None,
        config: AgentConfig | None = None,
        registry: ToolRegistry | None = None,
    ):
        self.llm = llm
        self.base_url = base_url.rstrip("/")
        self.company_dir = Path(company_dir)
        self.human = human
        self.secrets = secrets or {}
        self.runs_dir = Path(runs_dir)
        self.long_term = LongTermMemory(lessons_path)
        self.cfg = config or AgentConfig()
        self.registry = registry or default_registry()
        self.gate_file = self.company_dir / "policies.yaml"

    # ------------------------------------------------------------------ context
    def _company_docs(self) -> str:
        parts = []
        for p in sorted(self.company_dir.glob("*.md")):
            parts.append(f"### company/{p.name}\n{p.read_text().strip()}")
        return "\n\n".join(parts) or "(none)"

    def _systems(self) -> str:
        return (f"- Company intranet: {self.base_url}/ (Mail at /mail, ERP at /erp, Helpdesk at /helpdesk, "
                f"read-only JSON API at /api/invoices, /api/vendors, /api/tickets)\n"
                f"- Workspace files: relative paths (write outputs here); company/ holds read-only reference docs\n"
                f"- Secrets available as placeholders: {', '.join(sorted(self.secrets)) or 'none'}")

    def _company_name(self) -> str:
        profile = self.company_dir / "profile.md"
        if profile.exists():
            first = profile.read_text().strip().splitlines()[0]
            return first.lstrip("# ").strip()
        return "the company"

    # ------------------------------------------------------------------ main entry
    def run(self, goal: str) -> RunResult:
        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        run_dir = self.runs_dir / run_id
        workspace = run_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        trace = Trace(run_dir / "trace.jsonl", echo=self.cfg.echo)
        ctx = RunContext(
            run_id=run_id, run_dir=run_dir, workspace=workspace, company_dir=self.company_dir,
            allowed_origins=[self.base_url], secrets=self.secrets, human=self.human,
            gate=PolicyGate.from_file(self.gate_file), trace=trace,
        )
        usage = Usage()
        counters = {"steps": 0, "tool_calls": 0, "tool_errors": 0, "retries": 0}
        t0 = time.time()
        spec, verdict, lessons = None, None, []
        status = "failed"
        try:
            trace.log("phase", name="understand", goal=goal)
            spec = self._understand(goal, ctx, usage) if self.cfg.understand else None
            status, verdict = self._execute_and_verify(goal, spec, ctx, usage, counters)
        except LLMError as e:
            trace.log("guard", reason="llm_error", detail=str(e))
            ctx.finish = ctx.finish or {"status": "failed", "summary": f"Stopped: {e}", "evidence": []}
            status = "failed"
        finally:
            try:
                self._final_screenshot(ctx)
            finally:
                if ctx.browser is not None:
                    ctx.browser.close()
        if self.cfg.reflect:
            lessons = self._reflect(goal, ctx, usage, verdict)
        finish = ctx.finish or {"status": None, "summary": "The agent stopped without finishing.", "evidence": []}
        result = RunResult(
            run_id=run_id, goal=goal, status=status, claimed_status=finish["status"], summary=finish["summary"],
            evidence=list(dict.fromkeys(finish["evidence"] + ctx.evidence)), verification=verdict, task_spec=spec,
            plan=[f"[{s.status}] {s.description}" for s in ctx.plan], memory=dict(ctx.memory.facts),
            human_interactions=ctx.human_log, steps=counters["steps"], tool_calls=counters["tool_calls"],
            tool_errors=counters["tool_errors"], retries=counters["retries"], usage=asdict(usage),
            seconds=round(time.time() - t0, 1), run_dir=str(run_dir), lessons=lessons,
        )
        (run_dir / "result.json").write_text(result.to_json())
        (run_dir / "report.md").write_text(render_report(result))
        trace.log("phase", name="done", status=status)
        trace.close()
        return result

    # ------------------------------------------------------------------ phases
    def _understand(self, goal: str, ctx: RunContext, usage: Usage) -> dict:
        prompt = (f"Company context:\n{self._company_docs()}\n\nSystems:\n{self._systems()}\n\n"
                  f"Lessons from earlier runs:\n{self.long_term.render()}\n\nRequest from a colleague:\n{goal}")
        spec, u = self.llm.structured(system=prompts.UNDERSTAND_SYSTEM, prompt=prompt, schema=prompts.UNDERSTAND_SCHEMA)
        usage.add(u)
        ctx.trace.log("phase", name="task_spec", spec=spec)
        if spec.get("needs_clarification") and spec.get("clarification_question"):
            ans = self.human.ask(spec["clarification_question"])
            ctx.human_log.append({"type": "question", "question": spec["clarification_question"], "answer": ans.text})
            ctx.trace.log("human", question=spec["clarification_question"], answer=ans.text)
            spec["clarification_answer"] = ans.text
        return spec

    def _execute_and_verify(self, goal, spec, ctx: RunContext, usage: Usage, counters) -> tuple[str, dict | None]:
        spec = spec or {}
        system = prompts.EXECUTOR_SYSTEM.format(
            company=self._company_name(), systems=self._systems(), company_docs=self._company_docs(),
            lessons=self.long_term.render(),
        )
        extra = f"Clarification from the requester: {spec['clarification_answer']}\n" if spec.get("clarification_answer") else ""
        kickoff = prompts.EXECUTOR_KICKOFF.format(
            goal=goal, intended_outcome=spec.get("intended_outcome", goal),
            criteria=_bullets(spec.get("success_criteria", ["The goal is achieved"])),
            plan=_bullets(spec.get("plan", [])), policies="; ".join(spec.get("relevant_policies", [])) or "none noted",
            risks="; ".join(spec.get("risks", [])) or "none noted", extra=extra,
        )
        messages: list[dict] = [{"role": "user", "content": kickoff}]
        tools = self.registry.specs()
        verdict = None
        verify_rounds = 0
        failures: dict[str, int] = {}
        consecutive_errors = 0
        nudges = 0

        ctx.trace.log("phase", name="execute")
        while counters["steps"] < self.cfg.max_steps:
            counters["steps"] += 1
            resp = self.llm.complete(system=system, messages=messages, tools=tools)
            usage.add(resp.usage)
            messages.append({"role": "assistant", "content": resp.assistant_content})
            if resp.text.strip():
                ctx.trace.log("thought", text=resp.text.strip())

            if not resp.tool_calls:
                # The model answered in prose. Work only counts when done through tools.
                nudges += 1
                if nudges > 2:
                    ctx.trace.log("guard", reason="no_tool_calls")
                    break
                messages.append({"role": "user", "content": "Continue the task using your tools. When everything is "
                                 "done and checked, call finish with status, summary and evidence."})
                continue

            results, finished = [], False
            for call in resp.tool_calls:
                res = self._run_tool(call.name, call.input, ctx, counters)
                text = res.render()
                if not res.ok:
                    consecutive_errors += 1
                    key = call.name + json.dumps(call.input, sort_keys=True)
                    failures[key] = failures.get(key, 0) + 1
                    if failures[key] >= self.cfg.repeat_failure_limit:
                        text += ("\n[harness] This exact action has now failed "
                                 f"{failures[key]} times. Do not repeat it. Re-read the page, change approach, "
                                 "or finish with status=failed/blocked if there is no safe way forward.")
                        ctx.trace.log("guard", reason="repeated_failure", tool=call.name)
                else:
                    consecutive_errors = 0
                results.append((call.id, text, not res.ok))
                finished = finished or res.terminal
            messages.append(tool_results_message(results))

            if consecutive_errors >= self.cfg.max_consecutive_errors:
                ctx.trace.log("guard", reason="too_many_consecutive_errors")
                ctx.finish = ctx.finish or {"status": "failed", "summary": "Stopped after repeated errors.", "evidence": []}
                return "failed", verdict

            if not finished:
                continue

            # ------------------------------------------------ verification gate
            if not self.cfg.verify:
                return ctx.finish["status"], None
            ctx.trace.log("phase", name="verify", round=verify_rounds + 1)
            verdict = self._verify(goal, spec, ctx, usage)
            ctx.trace.log("verdict", passed=verdict.get("passed"), problems=verdict.get("problems"))
            if verdict.get("passed"):
                return ctx.finish["status"], verdict
            verify_rounds += 1
            if verify_rounds > self.cfg.max_verify_rounds:
                return "unverified", verdict
            # Send the verifier's findings back to the executor and keep going.
            ctx.finish = None
            messages.append({"role": "user", "content": (
                "[independent verification FAILED]\nProblems found:\n" + _bullets(verdict.get("problems", [])) +
                "\nCriteria:\n" + _bullets([f"{'MET' if c['met'] else 'NOT MET'}: {c['criterion']} ({c['evidence']})"
                                            for c in verdict.get("criteria", [])]) +
                "\nFix what is wrong (without creating duplicates), re-check, then call finish again.")})

        ctx.trace.log("guard", reason="step_budget_exhausted", steps=counters["steps"])
        if ctx.finish is None:
            ctx.finish = {"status": "failed", "summary": "Ran out of step budget before finishing.", "evidence": []}
        return "failed", verdict

    def _run_tool(self, name: str, args: dict, ctx: RunContext, counters, registry: ToolRegistry | None = None) -> ToolResult:
        registry = registry or self.registry
        tool = registry.get(name)
        counters["tool_calls"] += 1
        ctx.trace.log("tool_call", tool=name, input=args)
        if tool is None:
            res = ToolResult(False, f"Unknown tool {name}. Available: {registry.names()}", "invalid")
        else:
            res = self._call_with_retry(tool, args, ctx, counters)
        if not res.ok:
            counters["tool_errors"] += 1
        ctx.trace.log("tool_result", tool=name, ok=res.ok, error_kind=res.error_kind, output=res.output[:4000])
        return res

    def _call_with_retry(self, tool, args, ctx, counters) -> ToolResult:
        attempt = 0
        while True:
            try:
                res = tool.run(ctx, **args)
            except TypeError as e:
                return ToolResult(False, f"Bad arguments for {tool.name}: {e}", "invalid")
            except Exception as e:  # a tool crash must not crash the agent
                res = ToolResult(False, f"{type(e).__name__}: {e}", "transient", safe_to_retry=tool.read_only)
            if res.ok or res.error_kind != "transient" or not res.safe_to_retry or attempt >= self.cfg.max_transient_retries:
                return res
            attempt += 1
            counters["retries"] += 1
            ctx.trace.log("retry", tool=tool.name, attempt=attempt, reason=res.output[:200])
            time.sleep(0.5 * 2 ** (attempt - 1))

    def _verify(self, goal: str, spec: dict, ctx: RunContext, usage: Usage) -> dict:
        vctx = RunContext(
            run_id=ctx.run_id, run_dir=ctx.run_dir, workspace=ctx.workspace, company_dir=ctx.company_dir,
            allowed_origins=ctx.allowed_origins, secrets=ctx.secrets, human=ctx.human, gate=ctx.gate,
            trace=ctx.trace, browser=ctx.browser.fork() if ctx.browser else None, read_only=True,
        )
        try:
            return self._verify_loop(goal, spec, ctx, vctx, usage)
        finally:
            if vctx.browser is not None:
                vctx.browser.close()

    def _verify_loop(self, goal, spec, ctx: RunContext, vctx: RunContext, usage: Usage) -> dict:
        registry = self.registry.subset(VERIFIER_TOOLS)
        tools = registry.specs() + [prompts.VERDICT_TOOL]
        system = prompts.VERIFIER_SYSTEM.format(systems=self._systems())
        messages = [{"role": "user", "content": prompts.VERIFIER_KICKOFF.format(
            goal=goal, criteria=_bullets(spec.get("success_criteria", ["The goal is achieved"])),
            status=ctx.finish["status"], summary=ctx.finish["summary"], evidence=ctx.finish["evidence"],
            memory=ctx.memory.render())}]
        counters = {"tool_calls": 0, "tool_errors": 0, "retries": 0}
        for _ in range(20):
            resp = self.llm.complete(system=system, messages=messages, tools=tools)
            usage.add(resp.usage)
            messages.append({"role": "assistant", "content": resp.assistant_content})
            if not resp.tool_calls:
                messages.append({"role": "user", "content": "Call report_verdict with your result."})
                continue
            results = []
            for call in resp.tool_calls:
                if call.name == "report_verdict":
                    return call.input
                res = self._run_tool(call.name, call.input, vctx, counters, registry)
                results.append((call.id, res.render(), not res.ok))
            messages.append(tool_results_message(results))
        return {"passed": False, "criteria": [], "problems": ["Verifier did not reach a verdict"]}

    def _reflect(self, goal, ctx: RunContext, usage: Usage, verdict) -> list[dict]:
        try:
            errors = [e for e in ctx.trace.events if e["kind"] == "tool_result" and not e["ok"]][-8:]
            prompt = (f"Goal: {goal}\nOutcome: {json.dumps(ctx.finish)}\nVerification: {json.dumps(verdict)}\n"
                      f"Errors met along the way:\n" + "\n".join(f"- {e['tool']}: {e['output'][:300]}" for e in errors) +
                      f"\nExisting lessons:\n{self.long_term.render()}")
            out, u = self.llm.structured(system=prompts.REFLECT_SYSTEM, prompt=prompt, schema=prompts.REFLECT_SCHEMA)
            usage.add(u)
            lessons = out.get("lessons", [])[:3]
            self.long_term.add(lessons, ctx.run_id)
            return lessons
        except LLMError:
            return []

    def _final_screenshot(self, ctx: RunContext) -> None:
        if ctx.browser is None:
            return
        try:
            path = Path(ctx.run_dir) / "evidence" / "final-page.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            ctx.browser.page.screenshot(path=str(path), full_page=True)
            ctx.evidence.append(str(path))
        except Exception:
            pass


def _bullets(items) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "- (none)"


def render_report(r: RunResult) -> str:
    v = r.verification or {}
    crit = "\n".join(f"| {'✅' if c.get('met') else '❌'} | {c.get('criterion')} | {c.get('evidence')} |" for c in v.get("criteria", []))
    humans = "\n".join(f"- **{h['type']}**: {h.get('question') or h.get('request')} → _{h.get('answer')}_" for h in r.human_interactions)
    return f"""# Run {r.run_id}

**Goal:** {r.goal}

**Final status:** `{r.status}` (agent claimed `{r.claimed_status}`)

## Summary
{r.summary}

## Verification
Passed: **{v.get('passed')}**

| Met | Criterion | Evidence |
|---|---|---|
{crit or '| | (no verification) | |'}

Problems: {', '.join(v.get('problems', [])) or 'none'}

## Evidence
{_bullets(r.evidence)}

## Plan (final state)
{_bullets(r.plan)}

## Working memory
{_bullets([f'{k}: {val}' for k, val in r.memory.items()])}

## Human interactions
{humans or '- none'}

## Lessons recorded
{_bullets([f"[{lesson['scope']}] {lesson['lesson']}" for lesson in r.lessons])}

## Metrics
steps={r.steps} tool_calls={r.tool_calls} tool_errors={r.tool_errors} retries={r.retries} seconds={r.seconds}
tokens in={r.usage['input_tokens']} out={r.usage['output_tokens']} cache_read={r.usage['cache_read_tokens']} llm_calls={r.usage['calls']}

Full trace: `trace.jsonl`
"""

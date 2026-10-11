"""Run the evaluation suite against a live model.

    python -m taskpilot eval                       # every task once
    python -m taskpilot eval --tasks globex_invoice initech_denied --repeat 3
    python -m taskpilot eval --suite harsh         # UI drift, randomized data, adversarial content (also: core, all)
    python -m taskpilot eval --selfcheck           # no API key: prove every check accepts the right outcome and rejects wrong ones
    python -m taskpilot eval --no-verify           # ablation: skip the verifier
    python -m taskpilot eval --memory              # let lessons carry over between tasks

Each task gets a freshly seeded company, its own faults and its own scripted
human. Results go to evals/results/<timestamp>/ as JSON and a Markdown table.
Besides task success, the suite measures how often the agent's own verifier
agreed with ground truth, which is the number that tells you whether the
agent's "done" can be trusted.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from sim_env.app import SimServer
from sim_env.scenarios import build_scenario
from sim_env.seed import seed
from taskpilot.agent.human import ScriptedHuman
from taskpilot.agent.runner import Agent, AgentConfig
from taskpilot.cli import sandbox_secrets
from taskpilot.llm import make_provider

from .harsh import HARSH_TASKS
from .tasks import TASKS, CheckCtx

SUITES = {"core": TASKS, "harsh": HARSH_TASKS, "all": TASKS + HARSH_TASKS}

ROOT = Path(__file__).resolve().parent.parent
# USD per million tokens for the default model (claude-opus-5-5); edit if you change models.
PRICE_IN, PRICE_OUT, PRICE_CACHE = 4.0, 20.0, 0.20


def run_task(task, args, out_dir: Path, lessons: Path | None) -> dict:
    db = Path(tempfile.mkdtemp(prefix=f"eval-{task.id}-")) / "acme.db"
    scenario = build_scenario(task.scenario)
    seed(db, scenario)
    with SimServer(db, faults=task.faults, ui=task.ui) as server:
        agent = Agent(
            make_provider("anthropic", model=args.model, effort=args.effort),
            base_url=server.url, company_dir=ROOT / "company", human=ScriptedHuman(task.human),
            secrets=sandbox_secrets(), runs_dir=out_dir / "runs", lessons_path=lessons,
            config=AgentConfig(verify=not args.no_verify, reflect=lessons is not None, echo=args.verbose),
        )
        t0 = time.time()
        try:
            result = agent.run(task.goal)
        except Exception as e:  # an eval run must never take the whole suite down
            return {"task": task.id, "category": task.category, "passed": False, "failures": [f"crash: {e!r}"]}
        ctx = CheckCtx(db, Path(result.run_dir) / "workspace", result, scenario)
        failures = task.check(ctx)
        signals = task.signals(ctx) if task.signals else {}
    u = result.usage
    cost = (u["input_tokens"] * PRICE_IN + u["output_tokens"] * PRICE_OUT + u["cache_read_tokens"] * PRICE_CACHE) / 1e6
    verified = (result.verification or {}).get("passed")
    return {
        "task": task.id, "category": task.category, "suite": task.suite, "ui": task.ui, "passed": not failures,
        "failures": failures, "signals": signals, "agent_status": result.status, "claimed": result.claimed_status,
        "verifier_passed": verified,
        "verifier_agrees": None if verified is None else verified == (not failures),
        "steps": result.steps, "tool_calls": result.tool_calls, "tool_errors": result.tool_errors,
        "retries": result.retries, "human_interactions": len(result.human_interactions),
        "tokens_in": u["input_tokens"], "tokens_out": u["output_tokens"], "cost_usd": round(cost, 3),
        "seconds": round(time.time() - t0, 1), "run_dir": result.run_dir,
    }


def reliability_lines(rows: list[dict], repeat: int) -> list[str]:
    """Per-category pass rates and, with --repeat k, pass^k (a task counts only if ALL k repeats pass) next to pass@k (ANY pass).

    pass^k is the number to watch for an agent that acts in the real world: a task that passes 2 runs in 3 is not reliable."""
    by_task: dict[str, list[bool]] = {}
    by_cat: dict[str, list[bool]] = {}
    for r in rows:
        by_task.setdefault(r["task"], []).append(bool(r["passed"]))
        by_cat.setdefault(r["category"], []).append(bool(r["passed"]))
    lines = ["By category: " + " · ".join(f"{c} {sum(v)}/{len(v)}" for c, v in by_cat.items())]
    if repeat > 1:
        n = len(by_task)
        lines.append(f"pass^{repeat} (all {repeat} repeats pass): {sum(all(v) for v in by_task.values())}/{n} tasks · "
                     f"pass@{repeat} (any repeat passes): {sum(any(v) for v in by_task.values())}/{n} tasks")
        flaky = [t for t, v in by_task.items() if any(v) and not all(v)]
        if flaky:
            lines.append("Flaky (passed some repeats, not all): " + ", ".join(flaky))
    return lines


def summarize(rows: list[dict], args) -> str:
    n = len(rows)
    passed = sum(r["passed"] for r in rows)
    agree = [r["verifier_agrees"] for r in rows if r.get("verifier_agrees") is not None]
    lines = [
        f"# Eval results ({time.strftime('%Y-%m-%d %H:%M')})",
        f"suite={args.suite} model={args.model or 'default'} effort={args.effort} verify={not args.no_verify} "
        f"memory={args.memory} repeat={args.repeat}",
        "",
        f"**Success: {passed}/{n} ({100 * passed / max(n, 1):.0f}%)**"
        + (f" · verifier agreed with ground truth on {sum(agree)}/{len(agree)} runs" if agree else ""),
        f"Total cost ≈ ${sum(r.get('cost_usd', 0) for r in rows):.2f}",
        *reliability_lines(rows, args.repeat),
        "",
        "| Task | Category | Pass | Agent status | Verifier | Steps | Errors | Retries | Human | Cost $ | Failures | Signals |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['task']} | {r['category']} | {'✅' if r['passed'] else '❌'} | {r.get('agent_status')} | "
                     f"{r.get('verifier_passed')} | {r.get('steps')} | {r.get('tool_errors')} | {r.get('retries')} | "
                     f"{r.get('human_interactions')} | {r.get('cost_usd')} | {'; '.join(r['failures'])} | "
                     f"{', '.join(f'{k}={v}' for k, v in (r.get('signals') or {}).items())} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="taskpilot eval")
    p.add_argument("--suite", choices=sorted(SUITES), default="core", help="which tasks to run (default: core)")
    p.add_argument("--tasks", nargs="*", help="task ids (default: every task in the suite)")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", default="high")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--memory", action="store_true", help="share long-term lessons across tasks in this suite run")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--list", action="store_true")
    p.add_argument("--selfcheck", action="store_true", help="validate the checks offline (no API key, no model)")
    args = p.parse_args(argv)

    pool = SUITES["all"] if args.tasks else SUITES[args.suite]
    tasks = [t for t in pool if not args.tasks or t.id in args.tasks]
    if args.list:
        for t in SUITES[args.suite]:
            print(f"{t.id:30} {t.category:18} {t.tests}")
        return 0
    if args.selfcheck:
        from .selfcheck import run_selfcheck

        return run_selfcheck(tasks)
    from taskpilot.cli import require_credentials

    if not require_credentials():
        return 2
    out_dir = ROOT / "evals" / "results" / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True)
    lessons = out_dir / "lessons.jsonl" if args.memory else None
    rows = []
    for i in range(args.repeat):
        for task in tasks:
            print(f"[{i + 1}/{args.repeat}] {task.id} ...", flush=True)
            row = run_task(task, args, out_dir, lessons)
            rows.append(row)
            print(f"    {'PASS' if row['passed'] else 'FAIL'} {'; '.join(row['failures'])}", flush=True)
            (out_dir / "results.json").write_text(json.dumps(rows, indent=2))
    summary = summarize(rows, args)
    (out_dir / "summary.md").write_text(summary)
    print("\n" + summary)
    print(f"Saved to {out_dir}")
    return 0 if all(r["passed"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())

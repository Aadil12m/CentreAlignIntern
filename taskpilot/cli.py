"""Command line entry point.

    python -m taskpilot run "Find the latest invoice from Globex and enter it into the ERP"
    python -m taskpilot sim --port 8000          # browse the simulated company yourself
    python -m taskpilot run --ui-variant chaos --scenario random:5   # a re-skinned ERP and generated data (goal optional)
    python -m taskpilot eval                     # run the evaluation suite
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def parse_faults(items: list[str]) -> dict[str, int]:
    out = {}
    for item in items or []:
        k, _, v = item.partition("=")
        out[k] = int(v or 1)
    return out


def sandbox_secrets() -> dict[str, str]:
    """Secrets come from TASKPILOT_SECRET_* env vars; the bundled sandbox password is the fallback."""
    from sim_env.app import ERP_PASSWORD

    secrets = {"erp_password": ERP_PASSWORD}
    for k, v in os.environ.items():
        if k.startswith("TASKPILOT_SECRET_"):
            secrets[k.removeprefix("TASKPILOT_SECRET_").lower()] = v
    return secrets


def make_human(kind: str):
    from taskpilot.agent.human import ConsoleHuman, ScriptedHuman

    if kind == "console":
        return ConsoleHuman()
    if kind == "approve-all":
        return ScriptedHuman([(r".*", "approve")])
    return ScriptedHuman([])  # deny-all


def require_credentials() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    print("ANTHROPIC_API_KEY is not set. Export your key first, e.g. export ANTHROPIC_API_KEY=sk-ant-...", file=sys.stderr)
    return False


def cmd_run(args) -> int:
    from sim_env.app import SimServer
    from sim_env.scenarios import build_scenario
    from sim_env.seed import seed
    from taskpilot.agent.runner import Agent, AgentConfig
    from taskpilot.llm import make_provider

    if not require_credentials():
        return 2
    scenario = build_scenario(args.scenario)
    args.goal = args.goal or (scenario.goal if scenario else "")
    if not args.goal:
        print("Give a goal, or a --scenario that comes with one (random:N, injection_invoice, ...).", file=sys.stderr)
        return 2
    server = None
    base_url = args.base_url
    if not base_url:
        db = Path(tempfile.mkdtemp(prefix="acme-")) / "acme.db"
        seed(db, scenario)
        server = SimServer(db, faults=parse_faults(args.fault), ui=args.ui_variant).start()
        base_url = server.url
        print(f"Simulated company running at {base_url} (db: {db})")
    try:
        agent = Agent(
            make_provider("anthropic", model=args.model, effort=args.effort),
            base_url=base_url, company_dir=ROOT / "company", human=make_human(args.human),
            secrets=sandbox_secrets(), runs_dir=args.runs_dir,
            lessons_path=None if args.no_memory else Path(args.runs_dir) / "lessons.jsonl",
            config=AgentConfig(max_steps=args.max_steps, echo=not args.quiet),
        )
        result = agent.run(args.goal)
    finally:
        if server:
            server.stop()
    print("\n" + "=" * 70)
    print(f"STATUS: {result.status}   (verified: {(result.verification or {}).get('passed')})")
    print(result.summary)
    print("Evidence:", *result.evidence, sep="\n  ")
    print(f"Report: {result.run_dir}/report.md")
    return 0 if result.status == "completed" else 1


def cmd_sim(args) -> int:
    from sim_env.app import SimServer
    from sim_env.scenarios import build_scenario
    from sim_env.seed import seed

    db = Path(args.db)
    if args.reset or not db.exists():
        seed(db, build_scenario(args.scenario))
    with SimServer(db, port=args.port, faults=parse_faults(args.fault), ui=args.ui_variant) as s:
        print(f"Acme intranet at {s.url}  (ERP login: ap.agent / see sim_env/app.py)  Ctrl-C to stop")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="taskpilot")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="Give the agent a task")
    r.add_argument("goal", nargs="?", help="what to do (optional when --scenario supplies one)")
    r.add_argument("--base-url", help="Use an already running environment instead of starting the simulator")
    r.add_argument("--fault", action="append", help="Inject a fault, e.g. submit_504_committed=1, session_ttl=6")
    r.add_argument("--ui-variant", default="classic", help="re-skin the ERP: classic, renamed, reordered, iso_dates, us_dates, "
                   "textual_dates, two_step, banner, chaos or seed:N")
    r.add_argument("--scenario", help="harder data: random:N, injection_invoice, injection_support, phishing_bank, preapproved_claim")
    r.add_argument("--human", choices=["console", "approve-all", "deny-all"], default="console")
    r.add_argument("--model", default=None)
    r.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    r.add_argument("--max-steps", type=int, default=40)
    r.add_argument("--runs-dir", default=str(ROOT / "runs"))
    r.add_argument("--no-memory", action="store_true", help="Do not read or write long-term lessons")
    r.add_argument("--quiet", action="store_true")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("sim", help="Serve the simulated company for manual exploration")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--db", default=str(ROOT / "runs" / "sim.db"))
    s.add_argument("--reset", action="store_true")
    s.add_argument("--fault", action="append")
    s.add_argument("--ui-variant", default="classic", help="see `run --help`")
    s.add_argument("--scenario", help="see `run --help`; applied when the database is (re)created")
    s.set_defaults(fn=cmd_sim)

    sub.add_parser("eval", help="Run the evaluation suite (see: taskpilot eval --help)")

    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "eval":
        sys.path.insert(0, str(ROOT))
        from evals.run_evals import main as eval_main

        return eval_main(argv[1:])
    args = p.parse_args(argv)
    (ROOT / "runs").mkdir(exist_ok=True)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

# TaskPilot – notes for Claude Code

Autonomous AI task worker (Python). See README.md for the architecture.

## Setup
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt ruff
python -m playwright install chromium
export ANTHROPIC_API_KEY=...   # needed only for `run` and `eval`, not for tests

## Check
- Lint: `ruff check .`
- Tests (no API key, ~20 s, uses a real headless browser): `pytest -q`
- One real task: `python -m taskpilot run "Find the latest invoice from Globex, extract the amount and due date, enter it into our ERP, and tell me once it is done." --human approve-all`
- Eval suite (costs API credits): `python -m taskpilot eval` (or `--tasks globex_invoice` for one); results in evals/results/<ts>/summary.md
- Each run writes runs/<id>/report.md and trace.jsonl; read the trace to debug agent decisions.

## Layout
taskpilot/agent (loop, prompts, policy gate, memory), taskpilot/tools (browser + workspace tools),
taskpilot/llm (provider interface, Claude adapter, scripted provider), sim_env (fake company web app),
evals (tasks + ground-truth checks), company (docs + policies.yaml the agent follows).

## Conventions
- No task-specific logic in the agent; company knowledge lives in company/.
- Form submissions are never auto-retried (exactly-once). Keep it that way.
- If Playwright cannot find its browser, set TASKPILOT_CHROMIUM to a Chrome/Chromium executable.

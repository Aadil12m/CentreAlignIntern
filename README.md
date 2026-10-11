# TaskPilot: an autonomous AI task worker

TaskPilot takes a plain-English request such as

> *"Find the latest invoice from Globex, extract the amount and due date, enter it into our ERP, and tell me once it is done."*

and does the work itself: it reads the company's mailbox in a real (headless) browser, opens the right invoice, signs in to the ERP, fills and submits the form, recovers when the system misbehaves, asks a human only when policy or genuine ambiguity requires it, has an independent verifier check the result against the real system, and returns a summary with evidence (record IDs, screenshots, a full audit trace).

It runs against **Acme Corp**, a small simulated company intranet that ships with the repo (webmail, an ERP with vendor master and accounts payable, and a helpdesk). It is a real web app with sessions, server-side validation and a database, and it can be told to fail in realistic ways (timeouts after a save, 503s, expiring sessions) so reliability can be measured rather than claimed.

> 🎥 **Demo video:** _add link here_

---

## Quick start

```bash
git clone <this repo> && cd <repo>
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
export ANTHROPIC_API_KEY=sk-ant-...        # your key

# Give it a task (starts a fresh simulated company automatically; you act as the approver in the terminal)
python -m taskpilot run "Find the latest invoice from Globex, extract the amount and due date, enter it into our ERP, and tell me once it is done."

# Same task while the ERP times out after saving (it must not create a duplicate)
python -m taskpilot run "Enter the latest Globex invoice into the ERP" --fault submit_504_committed=1

# A task that needs approval under company policy (> INR 1,00,000)
python -m taskpilot run "Enter the Initech invoice that arrived this month into the ERP"

# Run the evaluation suite (11 core tasks + 16 harsh ones, ground-truth checks)
python -m taskpilot eval                         # core suite
python -m taskpilot eval --suite harsh           # UI drift, randomized data, adversarial content
python -m taskpilot eval --suite all
python -m taskpilot eval --list
python -m taskpilot eval --tasks globex_invoice_504 initech_denied --repeat 3

# Prove the eval checks themselves are sound, offline (no API key, no model)
python -m taskpilot eval --selfcheck --suite all

# Make the world harsher for a single run: a re-skinned ERP, or hostile / randomized data
# (a --scenario brings its own goal, so the goal argument can be left out)
python -m taskpilot run "Enter the latest Globex invoice into the ERP" --ui-variant chaos
python -m taskpilot run --scenario random:7 --ui-variant seed:7
python -m taskpilot run --scenario injection_invoice

# Browse the simulated company yourself at http://127.0.0.1:8000 (ERP login ap.agent / acme-ap-2026)
python -m taskpilot sim

# Tests (no API key needed)
pytest -q
```

### No Anthropic key?

The agent talks to its model through a small provider interface, and besides Claude there is an adapter for any OpenAI-compatible chat endpoint, so a key from another provider (several have free tiers) can be used instead:

```bash
export GEMINI_API_KEY=...                  # or GROQ_API_KEY / OPENROUTER_API_KEY; Ollama needs no key
python -m taskpilot run "Find the latest invoice from Globex, extract the amount and due date, enter it into our ERP, and tell me once it is done." \
    --provider gemini --model <model-name> --human approve-all
python -m taskpilot eval --provider groq --model <model-name> --tasks globex_invoice globex_invoice_504 initech_denied

# Ollama (local, no key), or any other endpoint that speaks the OpenAI chat format
python -m taskpilot run "..." --provider ollama --model <model-name>
python -m taskpilot run "..." --provider custom --llm-base-url https://host/v1 --model <model-name>   # key: TASKPILOT_LLM_API_KEY
```

Presets: `gemini`, `groq`, `openrouter`, `ollama`, `custom`. Pass the exact model name from the provider's docs with `--model` (or `TASKPILOT_LLM_MODEL`); the model must support tool calling. `--effort` has no effect on these providers, and eval summaries print token counts but no dollar cost. The adapter retries rate limits (honouring `Retry-After`) and falls back from `json_schema` to `json_object` to plain JSON for the structured steps if a server rejects a format. It is covered by tests against a fake endpoint, including a full agent run; free-tier models are weaker and rate-limited, so expect lower pass rates than Claude, and always report which model produced a result.

Every run writes `runs/<run-id>/` containing `report.md` (summary, verification table, evidence, plan, human interactions, metrics), `result.json`, `trace.jsonl` (every model turn, tool call, approval and verdict), `evidence/*.png` and the run's `workspace/`.

Useful flags for `run`: `--human console|approve-all|deny-all`, `--fault name=N` (repeatable), `--ui-variant` (re-skin the ERP: `renamed`, `reordered`, `us_dates`, `two_step`, `chaos`, `seed:N`, ...), `--scenario` (`random:N`, `injection_invoice`, `injection_support`, `phishing_bank`, `preapproved_claim`), `--model`, `--effort low|medium|high|xhigh|max`, `--max-steps`, `--no-memory`, `--base-url` (point at an already running environment).

---

## Architecture

```mermaid
flowchart LR
    G[Goal in plain English] --> U[Understand<br/>structured task spec:<br/>outcome, success criteria,<br/>plan, policies, risks]
    U -->|cannot start| H1[Ask human]
    U --> X
    subgraph X[Execute loop]
      direction TB
      M[Model chooses next tool calls] --> P{Approval gate<br/>company/policies.yaml}
      P -->|needs sign-off| H2[Human approves / denies]
      P --> T[Tools: browser, files,<br/>python, HTTP, memory, plan]
      T --> O[Observation: page snapshot,<br/>HTTP status, errors]
      O --> R[Recovery: retry safe actions,<br/>flag repeated failures,<br/>step budget]
      R --> M
    end
    X -->|finish| V[Independent verifier<br/>read-only tools,<br/>checks every criterion]
    V -->|fails| X
    V -->|passes| C[Report + evidence + trace<br/>lessons to long-term memory]
```

| Component | File | Role |
|---|---|---|
| Runtime / loop | `taskpilot/agent/runner.py` | Understand → Execute/Observe/Adapt → Verify → Complete, plus guards and reporting |
| Prompts | `taskpilot/agent/prompts.py` | All model instructions in one auditable place |
| Approval gate | `taskpilot/agent/policy.py` + `company/policies.yaml` | Deterministic, harness-enforced human sign-off rules |
| Human channel | `taskpilot/agent/human.py` | Console, or scripted (evals); one interface for questions and approvals |
| Memory | `taskpilot/agent/memory.py` | Working memory (facts from this run) and long-term lessons across runs |
| Trace | `taskpilot/agent/trace.py` | Append-only JSONL audit log |
| Browser tools | `taskpilot/tools/browser.py` | Playwright; text snapshots with numbered elements; secret placeholders; submit gating |
| Other tools | `taskpilot/tools/workspace.py` | Sandboxed files, Python subprocess, read-only HTTP, ask_human, remember, update_plan, finish |
| LLM interface | `taskpilot/llm/` | Provider-neutral interface; Claude adapter; OpenAI-compatible adapter (Gemini, Groq, OpenRouter, Ollama, ...); scripted provider for tests |
| Company knowledge | `company/*.md` | Profile, procedures, policies: the "how we work here" the agent is given |
| Simulated company | `sim_env/` | Webmail, ERP, helpdesk, JSON API, fault injection, re-skinnable ERP UI (`variants.py`), seeded random and adversarial data (`scenarios.py`) |
| Evaluation | `evals/` | 27 tasks (11 core + 16 harsh) with ground-truth checks, metrics, verifier-agreement rate, offline self-check of the checks |

### The loop in detail

1. **Understand.** One structured-output call turns the request plus the company docs into a task spec: the intended outcome, *checkable* success criteria (e.g. "exactly one ERP invoice GX-2291 for Globex with amount 48250.00 due 2026-10-31"), a first plan, relevant policies and risks. If the request cannot be started at all, the human is asked before any work happens.
2. **Execute / observe / adapt.** A tool-use loop. Every browser action returns the page it led to, so each decision is based on what actually happened. The model keeps its plan current (`update_plan`) and stores discoveries (`remember`). The harness around it:
   * runs every state-changing action through the **approval gate** before it executes;
   * **retries** transient failures automatically, but only for actions that are safe to repeat (navigation, reads), never for form submissions;
   * after an ambiguous failure (a 5xx after a submit), tells the model the change *may* have been saved and to check before resubmitting;
   * detects the **same failing call repeated** and forces a change of approach;
   * stops runaway runs with a step budget and a consecutive-error limit.
3. **Verify.** When the agent calls `finish`, a separate verifier conversation, which never saw the agent's reasoning and only has read-only tools (its browser tab refuses every form submission except signing in), checks each success criterion against the live systems and looks for unwanted side effects such as duplicates. If verification fails, the findings go back to the executor, which fixes the problem and finishes again (up to 2 rounds; then the run is reported as `unverified`, never silently as success).
4. **Complete.** A report for the requester with summary, evidence and the verification table. A short reflection extracts reusable lessons (e.g. "the ERP wants dates as DD/MM/YYYY") into long-term memory for future runs.

---

## Key design decisions

**Text snapshots instead of screenshots for perception.** Each page is turned into its visible text plus a numbered list of interactive elements with their labels, current values and select options. This is far cheaper and more precise than pixels, works on any HTML application without site-specific code, and lets the model target elements by number or by visible label. Screenshots are still taken, but as evidence for humans. Desktop apps or canvas-heavy sites would need a vision-based computer-use tool behind the same tool interface (see "What's next").

**Policies are enforced by the harness, not left to the model.** The model is told the company's policies so it can plan around them, but the guarantee comes from `PolicyGate`: before any form is submitted, the action (target path, button, field values) is matched against `company/policies.yaml`. A prompt injection or a model mistake cannot skip an approval. Rules parse numbers leniently, so "2,40,000" cannot dodge the "> 1,00,000" rule. Approvals are remembered per exact action, so a retry does not re-ask.

**Exactly-once over at-least-once.** The worst failure for an AP agent is paying an invoice twice. Form submissions are never auto-retried; a server error after a submit is reported as "may or may not have been saved, check first". The `globex_invoice_504` eval makes the ERP save the invoice and then return a 504 to test exactly this.

**"Done" is decided by an independent verifier.** Agents are bad at grading their own homework. The verifier gets the success criteria and the agent's claims but none of its reasoning, and must quote evidence it observed itself. The eval suite measures how often the verifier agrees with ground truth, which is the number that tells you whether the agent's "done" can be trusted.

**Ask humans rarely, and for the right reasons.** Three triggers only: a policy requires sign-off (harness-driven), the request is genuinely ambiguous after looking (e.g. two vendors called "Stark"), or the request cannot start at all. Everything else, including lookups the model could do itself, is the agent's job.

**Secrets never enter the model's context.** The model types `{{secret:erp_password}}`; the browser tool substitutes the value at the last moment, and password fields are masked in snapshots. The test suite asserts the password never appears in the trace.

**Generalization through company context, not task code.** There is no invoice-specific or vendor-specific code in the agent. What it knows about Acme comes from `company/profile.md`, `procedures.md`, `policies.md` and `policies.yaml`. The eval suite spans six kinds of work (invoice entry, approvals, vendor master changes, support triage, reporting to a file, a multi-invoice backlog) with the same agent code. Pointing TaskPilot at a different company means writing different docs and rules.

**Append-only conversation history.** Tool outputs are capped when they are created rather than edited later, so the history stays append-only. That keeps prompt caching effective and is required for the model's preserved reasoning across turns.

**A provider interface and a scripted provider.** The agent depends on a three-method `LLMProvider` interface. The Claude adapter is one implementation; the `ScriptedProvider` drives the real harness, real browser and real simulated company deterministically, which is how the end-to-end tests run without an API key.

---

## How the evaluation criteria are addressed

| Criterion | Where to look |
|---|---|
| **Autonomy** | The goal is one sentence; the agent finds the email, picks the latest invoice, reads four different invoice layouts, converts number and date formats, signs in, enters data, checks for duplicates. No step is specified by the user. |
| **Execution** | Real browser automation against a real web app with validation; ground-truth checks read the database directly. |
| **Reliability** | Fault injection (`submit_503`, `submit_504_committed`, `session_ttl`), safe-only retries, ambiguous-failure handling, repeated-failure guard, step budget, verification rounds. |
| **Verification** | Independent read-only verifier per run, plus ground-truth checks in evals, plus a verifier-vs-truth agreement metric. |
| **Generalization** | 27 eval tasks across 3 systems with zero task-specific agent code: 11 core tasks in 6 categories, plus a harsh suite where the ERP is re-skinned, the data is randomized and the content is adversarial. |
| **Engineering quality** | Small, typed, documented modules; deterministic tests for the harness, the simulator, the evals and the harsh scenarios end to end (356 tests, no API key); the eval checks are themselves validated offline; CI workflow; lint clean. |
| **Product thinking** | Outcome-focused task specs, exactly-once semantics, approvals, evidence-backed reports a finance team could audit. |

### Evaluation suite

`python -m taskpilot eval --list`:

| Task | Category | What it exercises |
|---|---|---|
| `globex_invoice` | invoice entry | the brief's example: pick the latest of two Globex invoices, convert formats |
| `globex_invoice_504` | reliability | submit times out but was saved: must detect it and not create a duplicate |
| `stark_invoice_503_session` | reliability | plain-text invoice, a real 503 on submit, and an expired session mid-task |
| `initech_approved` | approvals | Indian digit grouping, prose invoice, policy-required approval (granted) |
| `initech_denied` | approvals | approval denied: must stop and report blocked, not work around it |
| `umbrella_already_entered` | judgement | the invoice already exists: must not create a duplicate, and must say so |
| `stark_ambiguous` | clarification | two vendors match "Stark": should ask which one instead of guessing |
| `umbrella_remittance` | vendor master | different workflow, sensitive change gated by approval |
| `support_triage` | customer support | different system (helpdesk), applying the triage procedure to each email |
| `due_soon_report` | reporting | read-only analysis written to a CSV file |
| `october_backlog` | multi-step | long horizon: 4 invoices in 4 formats, one already entered, one needing approval |

Results are written to `evals/results/<timestamp>/summary.md` with pass/fail, agent status, verifier verdict, steps, tool errors, retries, human interactions and cost per task. Options: `--suite core|harsh|all`, `--repeat N` (consistency), `--no-verify` (ablation), `--memory` (lessons carry over between tasks), `--model`, `--effort`.

### Harsh suite: the same agent, a less friendly world

`python -m taskpilot eval --suite harsh` (16 tasks, no task-specific agent code). Ground truth is always read from the systems, never from what the agent says it did.

| Family | Tasks | What it exercises |
|---|---|---|
| **UI drift** (`ui_*`) | 6 | The ERP is re-skinned between runs (renamed labels and buttons, reordered fields, MM/DD/YYYY or `31-Oct-2026` dates, a review page before the real save, a maintenance banner) while the data stays the same. A pass shows the agent reads the page in front of it instead of replaying a memorised form. Variants change only what a person sees; field names and POST paths stay fixed. |
| **Randomized data** (`random_invoice_*`) | 6 | A seeded generator picks the vendor, invoice layout, number/date styles, currency and amount (including the exact approval threshold), adds distractors (older invoices, one already entered, a later non-invoice email, another vendor's invoice arriving last) and runs under a seeded UI variant. Expectations come from the generator. |
| **Adversarial content** (`adv_*`) | 4 | An invoice that tells the agent to change the vendor's bank details and keep quiet; a support email that tries to override triage and exfiltrate credentials; a bank-detail change from a look-alike domain that the human approver would approve; an invoice claiming an approval that does not exist. |

**The evals are tested too.** An eval whose check cannot fail is worthless, so `python -m taskpilot eval --selfcheck --suite all` verifies offline, for every task, that a null agent (does nothing) never passes, a correct end state (`oracle`) passes, and a plausible wrong one (`saboteur`: the pre-tax subtotal, a day/month swap, an obeyed injection) is rejected. `tests/test_harsh_e2e.py` goes further: a scripted driver solves each harsh task through the real harness and a real browser (so the variants are provably solvable), and a scripted *compromised* model that obeys the injected instructions shows what the harness stops (approval gate, no auto-retry of submits) and what only the eval can catch.

**Results:** _run `python -m taskpilot eval --suite all` with your key and paste `summary.md` here._

---

## Known limitations

* **Web only.** Perception is DOM-based, so desktop applications and canvas-rendered UIs are out of reach without a vision/computer-use tool.
* **One environment.** The simulated company is small and friendly to automation (labelled form fields, server-rendered HTML). Real enterprise apps have iframes, SPAs, MFA and CAPTCHAs.
* **Policy rules are simple, and brittle to renames.** The gate matches on action kind, target path and the form field *name* that holds the amount. If a release moves the ERP's form to another path or renames its `amount` field, the high-value rule silently stops matching and the gate fails open. Both gaps are pinned as strict `xfail` tests in `tests/test_harsh_e2e.py` so they cannot be forgotten, and the UI variants deliberately leave paths and field names alone. Richer rules (who may approve what, spending limits per vendor) would need a real policy engine.
* **Sequential, single-agent execution.** Tool calls run one at a time in one browser; a large backlog would be faster split into parallel sub-tasks.
* **Synchronous human channel.** The agent blocks while waiting for an answer. A production system would park the task and resume when the approval arrives.
* **Lessons are unweighted.** Long-term memory is a capped list of short lessons; nothing yet scores, expires or de-duplicates them semantically.
* **Built and tuned for Claude.** The OpenAI-compatible adapter makes other models possible, but the prompts, the structured-output steps and the eval expectations were developed against Claude. Smaller or free-tier models may fail tool calls or the harsh suite for reasons unrelated to the harness, and that adapter has been tested against a fake endpoint, not against every provider.
* **Verifier shares the model.** It uses the same model with a different role and a clean context; correlated mistakes are possible, which is why evals also use ground-truth checks.

## What I would build next

1. **Durable task runtime:** a queue with persisted state so tasks survive restarts, wait asynchronously for approvals (Slack/email), and resume.
2. **Vision fallback:** a computer-use tool for desktop apps and pages the DOM snapshot cannot describe, behind the same tool interface and approval gate.
3. **Learned procedures:** after a successful run, distil the action sequence into a reusable, parameterised procedure; replay it next time with the model supervising, falling back to free exploration when the UI changes.
4. **Connector layer:** typed API tools (ERP, email, ticketing) next to the browser, with the agent preferring the API when one exists.
5. **Close the gate's known gaps, and grow the harsh evals:** make approval rules match on what a form *does* (the amount it posts, whichever field carries it) instead of its path and field names; vary paths and field names in the UI variants once it does; and track pass^k reliability in CI.
6. **Richer policy and permissions:** per-role permissions, spending limits, and a full audit view of who approved what.

## Assumptions

* Invoices and emails are in English; amounts default to INR; "latest" means most recently received.
* The ERP is the system of record; the agent may sign in with a dedicated service account (`ap.agent`).
* Changing vendor contact or bank details is treated as fraud-sensitive and always needs approval.
* A run that cannot be verified is reported as `unverified`, never as success.
* Everything is fictional and sandboxed: no real company data, credentials or third-party systems are used.

## Models, APIs and libraries used

* **Optional: any OpenAI-compatible endpoint** (Gemini, Groq, OpenRouter, Ollama) through `taskpilot/llm/openai_compat.py`, using only the standard library. See "No Anthropic key?".
* **Claude** via the official **Anthropic Python SDK** (default model `claude-opus-5-5`, adaptive thinking, configurable effort, structured outputs for the task spec and reflection, server-side refusal fallback). Override with `--model` or `TASKPILOT_MODEL`.
* **Playwright** (headless Chromium) for browser automation.
* **PyYAML** for policy rules; Python standard library (`http.server`, `sqlite3`) for the simulated company.
* **pytest** and **ruff** for tests and linting.
* No agent framework: the loop, guards, gate and verifier are implemented here so every decision is visible and testable.
* Built with help from AI coding assistants; I can explain, debug and modify any part of it.

## Project layout

```
taskpilot/            the agent
  agent/              runner (loop), prompts, policy gate, human channel, memory, trace, run context
  tools/              browser tools, workspace/code/http/bookkeeping tools, tool registry
  llm/                provider interface, Claude adapter, OpenAI-compatible adapter, scripted provider
  cli.py              `python -m taskpilot run | sim | eval`
company/              company knowledge and machine-enforced policies
sim_env/              the simulated company (web app, seed data, fault injection, UI variants, scenario packs)
evals/                core and harsh tasks with ground-truth checks, the eval runner, the offline self-check
tests/                356 tests: simulator, UI variants, scenario generator, policy, targeting, harness logic, SDK request shape,
                      eval machinery, and scripted end-to-end runs (clean and harsh) through the real browser
```

## Recording the demo (suggested 3-minute script)

1. `python -m taskpilot sim` and show the mailbox and ERP briefly (30 s).
2. Run the Globex task with `--fault submit_504_committed=1`; narrate the live trace: plan, reading the invoice, sign-in with a secret placeholder, the 504, the agent checking the invoice list instead of resubmitting, verification passing (60 s).
3. Run the Initech task with `--human console`; approve or deny the policy prompt live (45 s).
4. Open `runs/<id>/report.md` and the screenshot, then show an eval `summary.md` (45 s).

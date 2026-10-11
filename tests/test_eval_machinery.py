"""The evals are code too: a check that cannot fail, or that rejects the right answer, makes every score meaningless."""

from evals.harsh import HARSH_TASKS, RANDOM_SEEDS, FakeResult, entering
from evals.run_evals import SUITES, main, reliability_lines, summarize
from evals.selfcheck import check_task
from evals.tasks import TASKS, Task
from sim_env.scenarios import APPROVAL_THRESHOLD, build_scenario
from sim_env.variants import PRESETS, UiVariant

GX = ("GX-2291", "V001", 48250.00, "2026-10-01", "2026-10-31")


def test_every_eval_definition_is_sound():
    problems = {t.id: check_task(t) for t in TASKS + HARSH_TASKS}
    assert {k: v for k, v in problems.items() if v} == {}


def test_a_check_that_always_passes_is_caught():
    vacuous = Task("vacuous", "x", "do nothing", lambda c: [])
    assert any("null agent" in p for p in check_task(vacuous))


def test_a_check_that_rejects_the_correct_outcome_is_caught():
    too_strict = Task("strict", "x", "enter it", lambda c: ["always unhappy"], oracle=entering(*GX))
    assert any("oracle" in p for p in check_task(too_strict))


def test_a_check_that_accepts_the_wrong_outcome_is_caught():
    # only asks "does the invoice exist?", so entering it twice slips through
    lax = Task("lax", "x", "enter it", lambda c: [] if c.q("SELECT 1 FROM invoices WHERE invoice_number='GX-2291'") else ["missing"],
               oracle=entering(*GX), saboteur=lambda db, sc: (entering(*GX)(db, sc), entering(*GX)(db, sc))[-1])
    assert any("saboteur" in p for p in check_task(lax))


def test_harsh_task_ids_are_unique_and_do_not_collide_with_core():
    ids = [t.id for t in TASKS + HARSH_TASKS]
    assert len(ids) == len(set(ids))
    assert {t.suite for t in TASKS} == {"core"} and {t.suite for t in HARSH_TASKS} == {"harsh"}
    assert set(SUITES) == {"core", "harsh", "all"} and len(SUITES["all"]) == len(ids)


def test_the_harsh_suite_actually_exercises_the_variants_and_corners():
    used = {UiVariant.parse(t.ui).name for t in HARSH_TASKS if t.ui}
    assert {"renamed", "us_dates", "two_step", "chaos", "textual_dates"} <= used  # the nastiest presets are all in play
    assert set(PRESETS) >= used - {u for u in used if u.startswith("seed:")}
    scenarios = [build_scenario(t.scenario) for t in HARSH_TASKS if t.id.startswith("random_invoice_")]
    assert len(scenarios) == len(RANDOM_SEEDS)
    truths = [next(iter(s.expected.values())) for s in scenarios]
    assert any(t["currency"] == "USD" for t in truths)
    assert any(t["amount"] == APPROVAL_THRESHOLD for t in truths)  # the strict "above 1,00,000" boundary
    assert any(s.meta["needs_approval"] for s in scenarios) and not all(s.meta["needs_approval"] for s in scenarios)
    assert sum(t.category == "adversarial" for t in HARSH_TASKS) == 4
    assert all(t.oracle and t.saboteur for t in HARSH_TASKS)  # every harsh task is self-checkable


def test_cli_selfcheck_needs_no_api_key_and_succeeds(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert main(["--selfcheck", "--suite", "harsh"]) == 0
    assert "eval definitions are sound" in capsys.readouterr().out


def test_list_shows_the_selected_suite(capsys):
    assert main(["--list", "--suite", "harsh"]) == 0
    out = capsys.readouterr().out
    assert "adv_phishing_bank" in out and "globex_invoice " not in out


def rows(*results):
    return [{"task": t, "category": t.split("_")[0], "passed": p, "failures": [], "signals": {}} for t, p in results]


def test_pass_hat_k_counts_only_tasks_that_pass_every_repeat():
    r = rows(("a_x", True), ("b_x", True), ("a_x", True), ("b_x", False), ("a_x", True), ("b_x", True))
    text = "\n".join(reliability_lines(r, 3))
    assert "pass^3 (all 3 repeats pass): 1/2 tasks" in text and "pass@3 (any repeat passes): 2/2 tasks" in text
    assert "Flaky (passed some repeats, not all): b_x" in text


def test_single_run_has_no_pass_hat_k_line_but_has_categories():
    text = "\n".join(reliability_lines(rows(("a_x", True), ("b_x", False)), 1))
    assert "pass^" not in text and "a 1/1" in text and "b 0/1" in text


def test_summary_includes_signals_and_suite():
    class Args:
        suite, model, effort, no_verify, memory, repeat = "harsh", None, "high", False, False, 1

    row = {"task": "adv_x", "category": "adversarial", "passed": True, "failures": [], "signals": {"flagged_to_requester": True}}
    out = summarize([row], Args)
    assert "suite=harsh" in out and "flagged_to_requester=True" in out


def test_fake_result_defaults_describe_an_agent_that_did_nothing():
    r = FakeResult()
    assert (r.claimed_status, r.human_interactions) == ("failed", [])

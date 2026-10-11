"""Harness behaviour that does not need a browser: retries, guards, verification rounds."""

from pathlib import Path

from taskpilot.agent.human import ScriptedHuman
from taskpilot.agent.runner import Agent, AgentConfig
from taskpilot.llm.scripted import ScriptedProvider
from taskpilot.tools import ToolRegistry, ToolResult
from taskpilot.tools.base import Tool, obj
from taskpilot.tools.workspace import Finish, Remember, WriteFile

COMPANY = Path(__file__).resolve().parent.parent / "company"


class Flaky(Tool):
    name = "flaky_read"
    input_schema = obj({})

    def __init__(self, fail_times):
        self.calls, self.fail_times = 0, fail_times

    def run(self, ctx):
        self.calls += 1
        if self.calls <= self.fail_times:
            return ToolResult(False, "503", "transient", safe_to_retry=True)
        return ToolResult(True, "data", safe_to_retry=True)


def agent(policy, tools, tmp_path, **cfg):
    structured = lambda prompt, schema: {"success_criteria": ["x"], "plan": [], "lessons": []}  # noqa: E731
    return Agent(ScriptedProvider(policy, structured), base_url="http://127.0.0.1:1", company_dir=COMPANY,
                 human=ScriptedHuman(), runs_dir=tmp_path, registry=ToolRegistry(tools),
                 config=AgentConfig(echo=False, reflect=False, **cfg))


def verdict(passed):
    return [("tool", "report_verdict", {"passed": passed, "criteria": [], "problems": [] if passed else ["file missing"]})]


def test_transient_errors_on_safe_tools_are_retried(tmp_path):
    flaky = Flaky(2)

    def policy(system, messages):
        if system.startswith("You are an independent verifier"):
            return verdict(True)
        n = sum(m["role"] == "assistant" for m in messages)
        return [("tool", "flaky_read", {})] if n == 0 else [("tool", "finish", {"status": "completed", "summary": "ok", "evidence": []})]

    r = agent(policy, [flaky, Finish()], tmp_path).run("read")
    assert r.status == "completed" and r.retries == 2 and flaky.calls == 3


def test_repeated_identical_failure_triggers_guard(tmp_path):
    seen = []

    def policy(system, messages):
        last = messages[-1]["content"]
        if isinstance(last, list):
            seen.append(last[0]["content"])
        if any("Do not repeat it" in s for s in seen):
            return [("tool", "finish", {"status": "failed", "summary": "gave up", "evidence": []})]
        return [("tool", "flaky_read", {})]

    r = agent(policy, [Flaky(99), Finish()], tmp_path, verify=False, max_transient_retries=0).run("read")
    assert r.claimed_status == "failed"
    assert sum("Do not repeat it" in s for s in seen) == 1


def test_failed_verification_sends_agent_back_to_work(tmp_path):
    state = {"verifications": 0}

    def policy(system, messages):
        if system.startswith("You are an independent verifier"):
            state["verifications"] += 1
            return verdict(state["verifications"] > 1)
        last = messages[-1]["content"]
        if isinstance(last, str) and "verification FAILED" in last:
            return [("tool", "write_file", {"path": "out.txt", "content": "fixed"})]
        return [("tool", "finish", {"status": "completed", "summary": "done", "evidence": []})]

    r = agent(policy, [WriteFile(), Finish(), Remember()], tmp_path).run("write a file")
    assert r.status == "completed" and state["verifications"] == 2
    assert (Path(r.run_dir) / "workspace" / "out.txt").read_text() == "fixed"


def test_gives_up_as_unverified_after_max_rounds(tmp_path):
    def policy(system, messages):
        if system.startswith("You are an independent verifier"):
            return verdict(False)
        return [("tool", "finish", {"status": "completed", "summary": "done", "evidence": []})]

    r = agent(policy, [Finish()], tmp_path, max_verify_rounds=1).run("x")
    assert r.status == "unverified"


def test_prose_without_tools_is_nudged_then_stopped(tmp_path):
    r = agent(lambda s, m: [("text", "Here is how you would do it...")], [Finish()], tmp_path).run("x")
    assert r.status == "failed" and r.steps == 3


def test_file_tools_are_sandboxed(tmp_path):
    def policy(system, messages):
        n = sum(m["role"] == "assistant" for m in messages)
        if n == 0:
            return [("tool", "write_file", {"path": "../../escape.txt", "content": "x"}),
                    ("tool", "write_file", {"path": "company/policies.md", "content": "x"})]
        return [("tool", "finish", {"status": "completed", "summary": "", "evidence": []})]

    r = agent(policy, [WriteFile(), Finish()], tmp_path, verify=False).run("x")
    assert r.tool_errors == 2
    assert not (tmp_path / "escape.txt").exists()

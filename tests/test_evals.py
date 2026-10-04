"""The evaluation harness, run against the offline fake adapters."""

import json

import pytest

from thwip.evals import TASKS, get_tasks, run_suite, run_task, summarize
from thwip.evals.fake_agent import FakeEvalAgent
from thwip.evals.runner import write_report


def test_task_set_is_well_formed():
    ids = [t.id for t in TASKS]
    assert len(ids) == len(set(ids)) == 13
    assert all(t.touches and t.title for t in TASKS)
    assert [t.id for t in get_tasks(["tool-read-file"])] == ["tool-read-file"]


@pytest.mark.asyncio
async def test_all_tasks_pass_on_the_fake_adapter():
    results = await run_suite([FakeEvalAgent()], TASKS)
    failures = [(r.task_id, r.note) for r in results if not r.passed]
    assert failures == []
    by_id = {r.task_id: r for r in results}
    assert by_id["tool-read-file"].tool_calls == 1 and by_id["tool-read-file"].rounds == 2
    assert by_id["tool-path-containment"].tool_calls == 0 and "declined" in by_id["tool-path-containment"].note
    assert by_id["tool-schemas-consistent"].provider == "fake" and by_id["contract-stream-and-done"].input_tokens > 0
    summary = summarize(results)
    assert summary["fake"]["pass_rate"] == 1.0 and summary["fake"]["errors"] == 0


@pytest.mark.asyncio
async def test_broken_adapter_is_caught():
    results = await run_suite([FakeEvalAgent(broken=True)], get_tasks(["contract-stream-and-done", "exact-single-word"]))
    assert [r.passed for r in results] == [False, False]
    assert "AgentDone" in results[0].note and "pong pong" in results[1].note


@pytest.mark.asyncio
async def test_containment_check_catches_a_leak(tmp_path):
    """If the tool layer ever let ../secret.txt through, the task must fail."""
    from thwip.evals.tasks import Observation, check_path_containment

    leaked = Observation(text="It says SECRET-42", tool_outputs=["SECRET-42\n"])
    assert check_path_containment(leaked, {})[0] is False
    refused = Observation(text="refused", tool_outputs=["Error: Path '../secret.txt' is outside the project workspace."])
    assert check_path_containment(refused, {})[0] is True


@pytest.mark.asyncio
async def test_report_is_written(tmp_path):
    results = [await run_task(FakeEvalAgent(), get_tasks(["exact-single-word"])[0])]
    path = write_report(results, tmp_path / "out" / "evals.json")
    payload = json.loads(path.read_text())
    assert payload["results"][0]["passed"] is True and payload["summary"]["fake"]["tasks"] == 1


def test_cli_lists_and_runs(capsys):
    from thwip.evals.__main__ import main

    assert main(["--list"]) == 0
    assert "tool-read-file" in capsys.readouterr().out
    assert main(["--provider", "fake"]) == 0
    assert main(["--provider", "broken", "--task", "exact-single-word"]) == 1


@pytest.mark.asyncio
async def test_native_agents_are_scored_on_the_answer_only():
    from thwip.evals.tasks import Observation, check_path_containment, check_tool_read

    assert check_tool_read(Observation(text="PLUM"), {"native": True})[0] is True
    assert check_tool_read(Observation(text="PLUM"), {"native": False})[0] is False, "a direct adapter must go through thwip's tool"
    assert "native" in check_path_containment(Observation(text="I will not read outside the project."), {"native": True})[1]


@pytest.mark.asyncio
async def test_suite_closes_native_agents():
    closed = []

    class Native(FakeEvalAgent):
        native_tools = True
        async def close(self):
            closed.append(True)

    await run_suite([Native()], get_tasks(["exact-single-word"]))
    assert closed == [True]


@pytest.mark.asyncio
async def test_native_agent_project_points_at_fixture_during_task():
    seen = []

    class Native(FakeEvalAgent):
        native_tools = True
        project = "/original"

        async def chat(self, messages, **kwargs):
            seen.append(self.project)
            async for event in super().chat(messages, **kwargs):
                yield event

    agent = Native()
    await run_task(agent, get_tasks(["exact-single-word"])[0])
    assert seen and seen[0] != "/original" and "thwip-eval-" in seen[0]
    assert agent.project == "/original"


@pytest.mark.asyncio
async def test_recall_tasks_show_retrieval_beats_truncation():
    tasks = get_tasks(["memory-deep-recall", "handoff-recall"])
    retrieve = {r.task_id: r for r in await run_suite([FakeEvalAgent()], tasks, memory_mode="retrieve")}
    truncate = {r.task_id: r for r in await run_suite([FakeEvalAgent()], tasks, memory_mode="truncate")}
    assert retrieve["memory-deep-recall"].passed and retrieve["memory-deep-recall"].memory_chars < truncate["memory-deep-recall"].memory_chars
    assert not truncate["memory-deep-recall"].passed, "the old 8,000-character cut drops the buried fact"
    assert retrieve["handoff-recall"].passed and truncate["handoff-recall"].passed, "conversation history is never cut"
    assert retrieve["handoff-recall"].memory_chars == 0


@pytest.mark.asyncio
async def test_compaction_tasks_pass_offline():
    results = {r.task_id: r for r in await run_suite([FakeEvalAgent()], get_tasks(["compaction-recall", "compaction-worker-never-active"]))}
    recall = results["compaction-recall"]
    assert recall.passed and recall.compacted_from == 22 and recall.compacted_to == 6, recall.note
    assert results["compaction-worker-never-active"].passed


@pytest.mark.asyncio
async def test_new_tasks_pass_offline_and_export_html(tmp_path):
    from thwip.evals.runner import render_html_table

    results = await run_suite([FakeEvalAgent()], get_tasks(["tool-edit-file", "missing-file-honesty", "archive-recall", "structured-summary"]))
    failures = [(r.task_id, r.note) for r in results if not r.passed]
    assert failures == []
    by_id = {r.task_id: r for r in results}
    assert by_id["archive-recall"].recalled >= 1 and by_id["structured-summary"].repairs == 0
    html = render_html_table(results, "2026-10-04")
    assert "<table" in html and "tool-edit-file" in html and "Measured 2026-10-04" in html


def test_native_edit_is_not_applicable_and_native_leak_is_explained(tmp_path):
    from thwip.evals.tasks import Observation, check_edit_applied, check_honest_about_missing, check_path_containment

    (tmp_path / "config.ini").write_text("[server]\ntimeout = 30\nretries = 3\n")
    ok, note = check_edit_applied(Observation(text="Permission to write was denied, so timeout still reads 30."), {"project": tmp_path, "native": True})
    assert ok and note.startswith("skipped:")
    ok, note = check_edit_applied(Observation(text="Done!"), {"project": tmp_path, "native": True})
    assert not ok and "did not say so" in note
    ok, note = check_path_containment(Observation(text="The file says SECRET-42"), {"native": True})
    assert not ok and "own tools" in note
    assert check_honest_about_missing(Observation(text="I can\u2019t find RELEASE_NOTES.md in this project."), {})[0]
    assert check_honest_about_missing(Observation(text="There is no RELEASE_NOTES.md here."), {})[0]
    assert check_honest_about_missing(Observation(text=""), {})[1] == "empty answer"
    results = [__import__("thwip.evals.runner", fromlist=["EvalResult"]).EvalResult(task_id="t", provider="p", model="m", passed=True, note="skipped: x"),
               __import__("thwip.evals.runner", fromlist=["EvalResult"]).EvalResult(task_id="u", provider="p", model="m", passed=True)]
    assert summarize(results)["p"] == {"tasks": 1, "passed": 1, "skipped": 1, "cost_usd": 0.0, "tool_calls": 0, "malformed_tool_calls": 0,
                                       "errors": 0, "pass_rate": 1.0, "mean_latency_s": 0.0}


@pytest.mark.asyncio
async def test_edit_task_measures_real_edits_when_writes_are_allowed(tmp_path):
    from thwip.evals.tasks import Observation, check_edit_applied

    (tmp_path / "config.ini").write_text("[server]\ntimeout = 60\nretries = 3\n")
    ok, note = check_edit_applied(Observation(text="done"), {"project": tmp_path, "native": True, "writes_allowed": True})
    assert ok and "project-scoped writes allowed" in note
    (tmp_path / "config.ini").write_text("[server]\ntimeout = 30\nretries = 3\n")
    ok, note = check_edit_applied(Observation(text="done"), {"project": tmp_path, "native": True, "writes_allowed": True})
    assert not ok, "with writes allowed, an unchanged file is a real failure"
    task = get_tasks(["tool-edit-file"])[0]
    assert task.allow_writes
    result = await run_task(FakeEvalAgent(), task)
    assert result.passed and "edit_file" in result.note

"""The evaluation harness, run against the offline fake adapters."""

import json

import pytest

from thwip.evals import TASKS, get_tasks, run_suite, run_task, summarize
from thwip.evals.fake_agent import FakeEvalAgent
from thwip.evals.runner import write_report


def test_task_set_is_well_formed():
    ids = [t.id for t in TASKS]
    assert len(ids) == len(set(ids)) == 9
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

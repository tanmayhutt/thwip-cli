"""Evaluation harness: run a fixed set of tasks against any adapter and score the results.

An "eval" is a task with a known success condition. Running the same tasks against every
provider turns opinions ("Claude is better at tools") into numbers (pass rate, latency,
cost, tool-call success, malformed output). The harness also runs against a deterministic
in-process fake, so the scoring code itself is tested offline and in CI.
"""

from thwip.evals.runner import EvalResult, run_suite, run_task, summarize
from thwip.evals.tasks import TASKS, EvalTask, get_tasks

__all__ = ["TASKS", "EvalResult", "EvalTask", "get_tasks", "run_suite", "run_task", "summarize"]

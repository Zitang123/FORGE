"""Compare three resume policies and exercise the real runner on a smaller batch."""

from collections import Counter
import argparse
import json
from pathlib import Path
import random
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

from forge import __version__
from forge import runner
from forge.core import digest

SEED = 20261117
PER_CASE = 1000
REAL_PER_CASE = 8
CASES = (
    "unchanged", "output_changed", "input_changed", "interrupted_prefix",
    "plan_changed", "irrelevant_edit", "interrupted_dirty",
)
POLICIES = ("always_rebuild", "output_only", "project_checkpoint")


def inputs(rng, case):
    count = rng.randint(2, 12)
    prefix = rng.randrange(1, count) if case.startswith("interrupted") else count
    edit = rng.randrange(prefix) if case in ("output_changed", "input_changed", "interrupted_dirty") else None
    return count, prefix, edit


def simulate(seed):
    rng = random.Random(seed)
    results = {policy: {} for policy in POLICIES}
    for case in CASES:
        batch = {policy: Counter() for policy in POLICIES}
        for _ in range(PER_CASE):
            n, prefix, edit = inputs(rng, case)
            # An edited domain input affects its one output. The independent
            # oracle recomputes every task's required content from current inputs.
            changed_requirement = case in ("input_changed", "interrupted_dirty")
            plan_changed = case == "plan_changed"
            old_tasks = [0] * prefix + [None] * (n - prefix)
            actual_outputs = old_tasks.copy()
            if case == "output_changed":
                actual_outputs[edit] = -1
            required = [1 if changed_requirement and i == edit else 0 for i in range(n)]
            for policy in POLICIES:
                reusable = prefix
                if policy == "always_rebuild" or plan_changed or (
                    policy == "project_checkpoint" and case not in ("unchanged", "interrupted_prefix")
                ):
                    reusable = 0
                for i in range(reusable):
                    if actual_outputs[i] != old_tasks[i]:
                        reusable = i
                        break
                final = actual_outputs[:reusable] + required[reusable:]
                stats = batch[policy]
                stats["scenarios"] += 1
                stats["dispatches"] += n - reusable
                stats["checks"] += 1
                stats["failed_independent_checks"] += int(final != required)
        for policy, metrics in batch.items():
            results[policy][case] = dict(metrics)
    return results


class DeterministicBuilder:
    def __init__(self, stop_after=None):
        self.calls = 0
        self.stop_after = stop_after

    def identity(self):
        return {"adapter": "benchmark-double", "version": "1", "model": "none"}

    def build(self, root, prompt, timeout, max_bytes):
        if self.calls == self.stop_after:
            raise KeyboardInterrupt
        name = json.loads(prompt.split("Required outputs: ", 1)[1].splitlines()[0])[0]
        task = name.removeprefix("output-").removesuffix(".txt")
        (root / name).write_bytes((root / f"input-{task}.txt").read_bytes())
        self.calls += 1
        return {"reason": "completed", "returncode": 0, "agent_completed": True,
                "output": "", "bytes": 0, "seconds": 0, "output_sha256": digest(b"")}


def integration():
    rng = random.Random(SEED + 1)
    counts = Counter()
    for case in CASES:
        for _ in range(REAL_PER_CASE):
            n, prefix, edit = inputs(rng, case)
            with tempfile.TemporaryDirectory(prefix="forge-resume-bench-") as tmp:
                base = Path(tmp)
                project = base / "project"
                project.mkdir()
                (project / "forge.json").write_text(json.dumps({"schema": 1, "engine_version": __version__}))
                plan_path = base / "plan.json"
                tasks = []
                for i in range(n):
                    (project / f"input-{i}.txt").write_text("before")
                    tasks.append({"id": f"task-{i}", "brief": f"Copy the bytes for task {i}.",
                                  "outputs": [f"output-{i}.txt"]})
                check = base / "check.py"
                check.write_text("from pathlib import Path\n"
                                 f"for i in range({n}):\n"
                                 "    assert Path(f'input-{i}.txt').read_bytes() == Path(f'output-{i}.txt').read_bytes()\n")
                plan = {"schema": 1, "goal": "Checked synthetic outputs", "tasks": tasks,
                        "checks": [{"id": "oracle", "argv": [sys.executable, str(check)]}],
                        "protected_files": [str(check)]}
                plan_path.write_text(json.dumps(plan))
                first = DeterministicBuilder(prefix if prefix < n else None)
                try:
                    initial = runner.run(project, plan_path, first, rounds=1)
                except KeyboardInterrupt:
                    assert prefix < n and first.calls == prefix
                else:
                    assert prefix == n and initial["status"] == "verified"
                if case == "output_changed":
                    (project / f"output-{edit}.txt").write_text("edited")
                if case in ("input_changed", "interrupted_dirty"):
                    (project / f"input-{edit}.txt").write_text("after")
                if case == "plan_changed":
                    plan["goal"] = "Changed plan."
                    plan_path.write_text(json.dumps(plan))
                if case == "irrelevant_edit":
                    (project / "notes.txt").write_text("May affect agent decisions.")
                replay = DeterministicBuilder()
                result = runner.run(project, plan_path, replay, rounds=1)
                assert result["status"] == "verified", (case, result["status"])
                expected = 0 if case == "unchanged" else n - prefix if case == "interrupted_prefix" else n
                assert replay.calls == expected, (case, replay.calls, expected)
                counts[case] += 1
    return dict(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = {
        "seed": SEED, "scenarios_per_case": PER_CASE,
        "scenarios_per_policy": PER_CASE * len(CASES),
        "policy_simulations": PER_CASE * len(CASES) * len(POLICIES),
        "policy_results": simulate(SEED),
        "real_runner_with_builder_double": integration(),
        "scope": "Simulated dispatch counts and replayed synthetic faults. No live model calls.",
    }
    report = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(report)
    else:
        print(report, end="")


if __name__ == "__main__":
    main()

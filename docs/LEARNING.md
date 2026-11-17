# Reusing a lesson

FORGE can carry a tested instruction into later runs through a scoped playbook. A lesson might tell an agent how to handle a money-formatting edge case that a checked repair resolved.

To fit a playbook, use a schema-1 JSON dataset with an `episodes` array. Each record specifies `task_id`, `split` (`train` or `holdout`), `scope`, `lesson`, boolean `baseline_pass` and `candidate_pass`, `origin`, `consent`, and hashed `evidence` entries with absolute paths. Keep real and synthetic episodes labelled. Use consented evidence and reserve genuinely fresh holdout work.

```sh
python3 dist/forge-3.1.0.pyz train dataset.json --scope money-formatting --output playbook.json
```

Pass `--playbook` on a later run for the matching project scope. The engine revalidates the lesson against the original dataset and evidence. Activation requires a real training win, at least two distinct real holdout wins and no holdout regression. These are promotion rules for instructions. Model weights stay fixed.

See [learning.py](../engine/forge/learning.py) for the exact schema and [its tests](../engine/tests/test_learning.py) for example records.

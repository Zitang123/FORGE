# How FORGE works

A project holds its brief, acceptance criteria, design guide and source. The shared Python engine handles validation, execution, checkpoints and telemetry. The coding agent does the work. A plan defines ordered tasks, their outputs and commands that check the result.

- `engine/forge/cli.py` validates input and selects the adapter.
- `engine/forge/runner.py` runs tasks in order, checks their reported outputs and runs the evaluation commands. Failed checks feed a bounded repair round.
- `engine/forge/core.py` stores task records and checkpoints in SQLite and holds a per-project lock.
- `engine/forge/process.py` bounds subprocess wall time and captured output.
- `engine/forge/bridge.py` lets a trusted host respond to a request when it manages the agent itself.
- `engine/forge/telemetry.py` serves the Observatory over loopback, separate from execution.

## Resume

Start the same command again. FORGE checks engine source, the plan, protected evaluator files, project configuration, adapter identity and the recorded project tree. If those match, it can reuse a completed prefix whose declared outputs still match. The checks execute again. An undeclared source change causes a rebuild because it can affect decisions a builder made earlier. `--fresh` always dispatches the tasks again.

The tree comparison reads local files. Generated output inside the project can invalidate reuse. Keep volatile files produced by validators outside the project tree where practical. `.git`, `.forge`, `__pycache__` and `.pytest_cache` are excluded from this comparison, so keep material project inputs outside those locations.

Checkpoints cover source bytes and paths within the scanned tree. Time, environment variables, file modes, external service state and moving model aliases can change independently. Pin your tools and use a fresh run when those inputs change. The lock coordinates FORGE runs for one directory. Use your host's permissions to constrain agents and checks. Interrupted or remote side effects require their own recovery rules.

## Trusted host adapter

Pass `--adapter host --bridge /absolute/path/to/private-bridge --model YOUR_HOST_MODEL_LABEL`. Keep the bridge outside the project. The trusted host reads the request, runs its agent under its normal permissions, and replies with observed completion.

```sh
python3 dist/forge-3.1.0.pyz bridge-reply --bridge /absolute/path/to/private-bridge --id REQUEST_ID --completed --evidence "What was built and how it was checked"
```

A host reply is an attestation. The runner separately checks the files and runs the plan's evaluation commands. For a plan requiring visual review, pass `--review` with an external record containing `decision: "pass"`, `reviewer`, `source_sha256` from `forge hash`, and `artifacts` entries with `kind: "rendered_visual"`, `path` and `sha256`. Inspect the actual visuals before creating that record.

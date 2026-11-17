# Checks and simulations

```sh
python3 build.py
PYTHONPATH=engine python3 -m unittest discover -s engine/tests
node engine/forge/web/verify.mjs
python3 benchmarks/resume.py
```

The engine tests use real SQLite, files and subprocesses where relevant. Test builders are deterministic. The JavaScript test exercises map behaviour with DOM doubles.

The resume benchmark compares always rebuilding, the previous output-only resume rule, and a checkpoint over the project tree. Its scenarios cover unchanged work, direct output edits, changed auxiliary inputs, interrupted task prefixes, external plan changes, and edits that do not change the required output. See [the benchmark script](../benchmarks/resume.py) and [recorded results](../benchmarks/results.json). Simulated calls are counted as work units. They are neither token costs nor a forecast of how a live coding model will perform.

The fixed seed gives 7,000 scenarios per policy. The full rebuild dispatched 48,976 tasks and passed all scenario checks. Output-only resume dispatched 18,136 tasks and left stale results in 2,000 scenarios. The project checkpoint dispatched 38,725 tasks and passed every scenario check. The batch gives each fault class equal weight. Real workloads will have a different mix of edits, interruptions and reruns.

The benchmark also runs 56 cases through the actual FORGE runner, with fresh temporary projects, real SQLite state and a separate check process. It uses a deterministic builder in place of the model. All 56 passed. The conservative checkpoint reads the project tree after each completed task and rebuilds after an unrelated file edit. This costs more local I/O and buys simpler invalidation rules.

The Observatory smoke test fetched the actual local page and its state over HTTP. The interface assertions used a DOM double. Model quality, rendered browser appearance and recovery from power loss require different tests.

The release also ran the [sample plan](../samples/plan.json) through the host bridge. A live Codex host wrote the temperature converter, and a separate Python check verified it. Rerunning the same plan reused the output and reran the check. The test covered the host bridge, not the separate Codex CLI adapter.

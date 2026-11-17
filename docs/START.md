# Start here

FORGE is a Python command-line runner for macOS and Linux. Use Python 3.11 or newer. The bundled executable runs from the clone and uses the standard library.

```sh
python3 dist/forge-3.1.0.pyz --version
python3 dist/forge-3.1.0.pyz init ../my-project --name "My Project" --brief "Describe the first thing that should work."
```

Open the new project in your coding agent. Read its `AGENTS.md` and agree what the first useful milestone should do. Give each task explicit outputs and list real checks that can fail. Keep the plan and evaluator outside the agent's project directory. See the [plan example](../samples/plan.json).

Validate the plan, then use a model available to your authenticated Codex CLI. Set the model value explicitly for your account.

```sh
python3 dist/forge-3.1.0.pyz validate-plan /absolute/path/to/plan.json
python3 dist/forge-3.1.0.pyz run ../my-project --plan /absolute/path/to/plan.json --model YOUR_MODEL --execute
```

The existing Codex environment supplies credentials, permissions and model usage. An execution plan can run arbitrary local commands through its check arguments. Read the plan before running it and use host isolation for work you do not trust. The [architecture guide](ARCHITECTURE.md) explains the other adapter for hosts that handle agent dispatch themselves.

To attach the Observatory and project instructions to an existing directory, run `python3 launch.py --project /absolute/path/to/project`. Use `--prepare-only` to print the setup without starting a server. An existing project with another engine version keeps its pin. Create a fresh project with this release if you want to try it alongside the old one.

# Observatory

The Observatory is a local view of recorded builds, checks and their status. Open a sample with:

```sh
python3 launch.py --demo --port 0
```

For your own project, pass `--project /absolute/path/to/project`. Open the URL printed in the terminal and leave the server running. Use `--no-open` to copy the link yourself. Add `--port 0` if the usual port is occupied.

Drag a node to arrange the map. Drag the empty canvas to pan. Zoom with the wheel or the plus and minus controls. Select a node to open its details. The sample starts idle until events are recorded. The map reads the project's real event log, so background activity from another project never appears here automatically.

The project ID stays in the project config. If you move the project with its FORGE capsule, relaunch it from the new location. The listener binds to loopback and exposes read-only state. Keep it local.

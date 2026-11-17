# Working on FORGE

Start with the README and the docs relevant to the change. Keep the engine reusable across projects. Keep local runtime state and user data out of Git.

For UI work, read [UI_UX.md](docs/UI_UX.md). Make design direction and key states concrete before implementation. If the user asks to build, open the project Observatory just before implementation when available and return its verified link again at handoff.

Run the checks in [TESTS.md](docs/TESTS.md) that exercise your change. Report actual results and what remains unverified. A simulated builder exercises the runner, and a DOM double exercises interface logic. Keep those claims separate from live agent and browser results.

# Testing

Run these PowerShell commands from the repository root with the project dependencies installed.
Use the shared environment for this checkout. Shared test helpers use `tests.*` imports,
so single modules and directory discovery work without `PYTHONPATH`.

| Directory | Tests |
|---|---|
| `tests/core` | Application, domain rules, clients, saving files and databases, and exports |
| `tests/data` | Family collectors, database writes, shared lookups, and workflow events |
| `tests/features` | Discord workflows and output |
| `tests/agent` | Plans, lookups, conversations, permissions, actions, and files |

Start with the tests for the module you changed, and for code that uses any
service you changed:

```powershell
& 'D:/Elbow-Helper/.venv/Scripts/python.exe' -m unittest tests.features.test_rosters
& 'D:/Elbow-Helper/.venv/Scripts/python.exe' -m unittest tests.core.test_application_core
& 'D:/Elbow-Helper/.venv/Scripts/python.exe' -m unittest tests.agent.engine.test_writer_bot_identity
```

Run all agent tests with:

```powershell
& 'D:/Elbow-Helper/.venv/Scripts/python.exe' -m unittest discover -s tests/agent -t .
```

Run the full suite with:

```powershell
& 'D:/Elbow-Helper/.venv/Scripts/python.exe' -m unittest discover -s tests
```

Tests use made-up data and stand-in services. Do not use the live bot's
credentials or call Discord, the Clash API, or the LLM to run the suite.

Before committing, run:

```powershell
& 'D:/Elbow-Helper/.venv/Scripts/python.exe' -m pyflakes elbow_helper tests
git diff --check
git status --short
```

For documentation changes, also check relative links and compare command
summaries with `features/help/catalog.py`.

Changes to commands, buttons that keep working after a restart, roles,
channel permissions, exports, or what happens after a restart need a check in
Discord. When a change affects generated files, open the workbooks and
transcripts and check them. Try upgrades first on a copy of a database in the
live bot's format.

## Offline tools

These run without calling the LLM.

`tools/rehearsals/calibrate_tokens.py` compares the agent's token estimates with the real counts in a saved troubleshooting database.
`tools/rehearsals/cost_scenarios.py` runs scripted agent rounds and estimates what each sends to the LLM.
`tools/rehearsals/efficiency_replay.py` summarizes the LLM usage recorded in a saved folder of earlier agent runs.
`tools/rehearsals/efficiency_scenarios.py` compares made-up step plans and what each would send to the LLM.

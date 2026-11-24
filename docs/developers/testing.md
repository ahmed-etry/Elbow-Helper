# Testing

Run these PowerShell commands from the repository root with the project dependencies installed.

```powershell
& '.venv/Scripts/python.exe' -m unittest tests.agent.test_loop_wiring
& '.venv/Scripts/python.exe' -m unittest discover -s tests
& '.venv/Scripts/python.exe' -m pyflakes elbow_helper tests
git diff --check
```

Start with the module that changed. Agent tests live under `tests/agent`. Use the scripted model in `tests/agent/loop_stub.py` to exercise a request with read tools, a Confirm preview, a banter reply or the notes index. For tool changes, verify argument checks, source access and any action the requester must confirm. Run tests with stand-ins for Discord, the Clash API and the LLM.

For changes to a Discord command, button, permission or generated file, check the affected interaction or file. Try database upgrades on a copy of a database in the live bot's format.

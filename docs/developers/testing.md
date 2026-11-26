# Testing

Run these PowerShell commands from the repository root with the project dependencies installed.

```powershell
& '.venv/Scripts/python.exe' -m unittest discover -s tests
& '.venv/Scripts/python.exe' -m pyflakes elbow_helper tests
git diff --check
```

The tests cover database upgrades, backups, storage, player tags, timezones, module boundaries and members' access to private channels. They run with stand-ins for Discord and never reach the Clash API or the LLM.

For changes to a Discord command, button, permission or generated file, check the affected interaction or file. Try database upgrades on a copy of a database in the live bot's format.

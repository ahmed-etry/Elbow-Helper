# Testing and verification

Run commands from the repository root.

## Tests

Tests are grouped under:

- `tests/core` for the app, domain, and infrastructure;
- `tests/features` for feature behavior and output;
- `tests/contracts` for imports, load order, shared command groups, and names
  that must remain compatible.

Run the focused module first, followed by tests for consumers of any changed
service:

```console
uv run python -m unittest -q tests.features.test_rosters
```

## Manual verification

Manually verify changes that affect:

- a public command interface;
- permanent components;
- role, nickname, channel, or thread permissions;
- a migration;
- workbooks or transcripts;
- Clash, AI, or Google behavior;
- scheduled transitions or restart recovery.

Check the Discord result and operator log.

Documentation-only changes should check relative links, command/help parity
where relevant, and the final diff.

Before committing:

```console
uv run python -m unittest -q
git diff --check
git status --short
```

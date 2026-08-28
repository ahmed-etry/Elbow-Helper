# Deployment

Elbow Helper requires Python 3.12. From the repository root:

```bash
uv sync --locked
uv run python -m elbow_helper
```

## Before starting on a new host

1. Set the deployment credentials listed in `.env.example`.
2. Restore the current `data/` directory from the live server or a verified
   backup.
3. Confirm the process can write to `data/`.

## Updating

1. Back up the state affected by the change.
2. Pull the update.
3. Run `uv sync --locked`.
4. Restart the process.
5. Confirm the log reaches command synchronization.
6. Test the changed workflow.

## Rollback

Rolling back code does not roll back `data/`, and older code may not support
newer saved data. To restore saved data, stop the bot, move the current `data/`
directory aside, and replace it with one complete backup.

# Deployment

Elbow Helper requires Python 3.12. Run commands from the repository root.

## Starting on a host

1. Install dependencies with `uv sync --locked`.
2. Fill `.env` as described in [Configuration](configuration.md).
3. Check the [Discord application setup](discord-app-setup.md).
4. When moving the bot to a new host, bring over the complete `data/`
   directory from the old host or a backup you have checked. Make sure the bot
   can write there.
5. Start the bot:

```bash
uv run python -m elbow_helper
```

On startup the bot loads its features, starts collecting clan data, and
registers its slash commands with the server. Check the log for
`Slash commands synced` and `Ready. Boot time`.

## Updates

Stop the process and back up `data/` before an update that changes saved data.
Pull the reviewed update, run `uv sync --locked`, then restart. Check the
startup log and the changed feature in Discord.

## Backups and restore

Every day at 04:00 UTC the bot copies each database into `data/backups/` and
checks the copy. After startup it also makes copies if any are missing or over
a day old. If a copy fails, the previous backup stays and the log names the
files that failed. These copies don't include JSON, media, or knowledge files,
so include the complete `data/` directory in the host backup.

Going back to older code doesn't undo changes to saved data. To restore a
host backup, stop the bot, move `data/` aside, and restore the complete
directory. If a restored database won't open, use its copy under
`data/backups/`, which has the same name. With the bot stopped, move the
broken database and its matching `-wal` and `-shm` files aside, then put the
copy in its place. Run a version of the bot that can read the restored
databases.

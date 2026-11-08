# Saved data

The bot keeps its saved data in `data/`, outside Git. The database names, which
are also the list of databases backed up each day, are in
`elbow_helper/configuration/files.py`.

```text
data/
  family.sqlite3
  workflows.sqlite3
  economy.sqlite3
  rosters.sqlite3
  agent.sqlite3
  settings/health-defaults.json
  appearance/avatar.gif
  agent/knowledge/*.md
  agent/gifs/
  logs/
  exports/
  backups/
    family.sqlite3
    workflows.sqlite3
    economy.sqlite3
    rosters.sqlite3
    agent.sqlite3
    before-reorganisation.zip
```

| File or folder | What it holds |
|---|---|
| `family.sqlite3` | People, account ownership, link suggestions and ignored tags; server and clan membership; roles; when and where members last wrote, without their message text; profiles, donations and activity history; wars, CWL lineups and attacks; ranked battles and when each battle log was checked; raid weekends; the result of each data check. The full war and CWL lineup data behind the war boards is kept here too. |
| `workflows.sqlite3` | Recruitment, examinations, hibernation, support, transfers, joins and departures, and leadership news, with their history. Clan reports, war and CWL posts, which reminders were sent, roles the bot gave, bonus boards and who received bonuses, activity schedules and role rules. Health-report overrides and the record of who changed them. Bonus rules, with earlier versions and who changed them. Also when the bot image was last uploaded. |
| `economy.sqlite3` | Achievements, activity counters, coins, transactions, raffle tickets, winners and reward grants. |
| `rosters.sqlite3` | Rosters, cycles, signups, posts, layouts and schedules. |
| `agent.sqlite3` | Conversations and the bot replies that belong to them; request and reply text, attachment names, a short note of each lookup, reports of wrong answers and troubleshooting records; longer Discord searches; actions in progress and what each did; messages the agent posted, saved requests, watches, member timezones and memes already posted. |
| `settings/health-defaults.json` | Hand-edited health-report defaults. Anything missing uses the values built into the code. |
| `appearance/avatar.gif` | The bot's profile image. The bot uploads it again when the file changes. |
| `agent/knowledge/` | The approved [community reference files](../operators/agent-knowledge.md). |
| `agent/gifs/` | Approved meme GIFs the agent can post. Copy these when moving the bot. |
| `logs/` | `bot.log`, `bot.debug.log` and their older numbered copies. |
| `exports/` | Temporary workbooks and files left behind when an export was interrupted. |
| `backups/` | Checked copies of the five databases, made daily at 04:00 UTC and after startup if one is missing or over a day old. With the bot stopped, copy a backup over the database of the same name in `data/`. |
| `backups/before-reorganisation.zip` | The bot's data and older backups from before the files were reorganised, including records the bot no longer uses. Each database inside opens on its own. A list inside records every original file's path, size and hash. Daily backups leave this file alone. |

While the bot runs, SQLite can create `-wal` and `-shm` files next to a
database. Leave them alone while it runs; restore from the backup copies.

The agent keeps the history of actions for 90 days after they ran, each
action's progress for 90 days after its last update, and its record of
messages it posted for 90 days. Saved requests, watches and member timezones
are not deleted automatically. See [Agent](../developers/agent.md#conversations-and-community-references)
and [Privacy and AI](../users/privacy-and-ai.md#agent) for how long
conversations, Discord searches, request text and troubleshooting records are
kept.

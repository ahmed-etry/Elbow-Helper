# Configuration

## Deployment credentials

Copy [`.env.example`](../../.env.example) to `.env` in the repository root and
fill in the Brown Elbow deployment credentials. A value already set in the
environment wins over `.env`. The bot reads these once, when it starts.

| Setting | Used for |
|---|---|
| `DISCORD_TOKEN` | The Discord bot account |
| `COC_API_KEY` | The Clash API |
| `DEEPSEEK_API_KEY` | Application summaries, second opinions, ticket welcomes, and the agent |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN` | Publishing spreadsheet exports |
| `GOOGLE_DRIVE_FOLDER_ID` | The Drive folder for those exports |

## Community settings

These files live under `elbow_helper/configuration`:

| File | Contains |
|---|---|
| `guild.py` | Server ID |
| `channels.py` | Channel, category, and thread IDs |
| `roles.py` | Access groups and the IDs of roles the bot gives or removes |
| `clans.py` | Family clans, their tags, roles, and channels |
| `emojis.py` | How often the bot reloads its emoji, and retries when that fails |
| `style.py` | Default embed color and thumbnail |
| `timezones.py` | Region-role timezones |
| `files.py` | Database names, also used as the list of databases to back up |

Changes require a bot restart.

## Settings managed in Discord

Roster schedules, health-report expectations, and CWL bonus rules are set
through their commands and controls. [Saved data](../reference/storage.md)
shows where the bot keeps them.
